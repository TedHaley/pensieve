// pensieve-media: decodes video and audio for the backend with AVFoundation, so the backend needs no ffmpeg.
//   pensieve-media info <file>                                   -> {"duration": s, "video": bool, "audio": bool}
//   pensieve-media frames <file> <dir> <start> <seconds> <fps> <max>  -> PNG frames in dir, one per 1/fps second
//   pensieve-media audio <file> <out> <start> <seconds>          -> mono float32 samples, raw, in out; prints the rate
// Times are in seconds; seconds 0 means to the end. Errors go to stderr with exit status 1.
import AVFoundation
import Foundation
import ImageIO
import UniformTypeIdentifiers

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func asset(_ path: String) -> AVURLAsset {
    AVURLAsset(url: URL(fileURLWithPath: path), options: [AVURLAssetPreferPreciseDurationAndTimingKey: true])
}

func sync<T>(_ work: @escaping () async throws -> T) throws -> T {
    var result: Result<T, Error>!
    let done = DispatchSemaphore(value: 0)
    Task {
        do { result = .success(try await work()) } catch { result = .failure(error) }
        done.signal()
    }
    done.wait()
    return try result.get()
}

func info(_ path: String) throws {
    let a = asset(path)
    let (duration, video, audio) = try sync {
        (try await a.load(.duration).seconds,
         try await !a.loadTracks(withMediaType: .video).isEmpty,
         try await !a.loadTracks(withMediaType: .audio).isEmpty)
    }
    print("{\"duration\": \(duration.isFinite ? duration : 0), \"video\": \(video), \"audio\": \(audio)}")
}

/// Frames at `fps` from `start` for `seconds`; at most `max`, spread evenly when there would be more.
func frames(_ path: String, _ dir: String, _ start: Double, _ seconds: Double, _ fps: Double, _ max: Int) throws {
    let a = asset(path)
    let duration = try sync { try await a.load(.duration).seconds }
    let end = seconds > 0 ? Swift.min(duration, start + seconds) : duration
    var times = stride(from: start, to: end, by: 1 / fps).map { $0 }
    if times.isEmpty { times = [start] }
    if times.count > max {
        times = (0..<max).map { times[Int((Double($0) * Double(times.count - 1) / Double(max - 1)).rounded(.down))] }
    }
    let gen = AVAssetImageGenerator(asset: a)
    gen.appliesPreferredTrackTransform = true
    gen.maximumSize = CGSize(width: 1280, height: 1280)  // the model reads at most 1,260 patches of 16 px
    gen.requestedTimeToleranceBefore = CMTime(seconds: 0.25, preferredTimescale: 600)
    gen.requestedTimeToleranceAfter = CMTime(seconds: 0.25, preferredTimescale: 600)
    // all times in one request: AVFoundation then decodes forward through the file instead of seeking for each frame
    let images: [CGImage] = try sync {
        var got: [CMTime: CGImage] = [:]
        for await r in gen.images(for: times.map { CMTime(seconds: $0, preferredTimescale: 600) }) {
            got[r.requestedTime] = try r.image
        }
        return times.compactMap { got[CMTime(seconds: $0, preferredTimescale: 600)] }
    }
    for (i, image) in images.enumerated() {
        let url = URL(fileURLWithPath: dir).appendingPathComponent(String(format: "frame%03d.png", i))
        guard let out = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            fail("can't write \(url.path)")
        }
        CGImageDestinationAddImage(out, image, nil)
        if !CGImageDestinationFinalize(out) { fail("can't write \(url.path)") }
    }
    print(images.count)
}

/// The sound from `start` for `seconds`, mixed to mono at the file's own sample rate, as raw float32. Prints the
/// sample rate and the number of samples.
func audio(_ path: String, _ outPath: String, _ start: Double, _ seconds: Double) throws {
    let a = asset(path)
    let tracks = try sync { try await a.loadTracks(withMediaType: .audio) }
    guard !tracks.isEmpty else { fail("no sound in \(path)") }
    let formats = try sync { try await tracks[0].load(.formatDescriptions) }
    let rate = formats.compactMap { CMAudioFormatDescriptionGetStreamBasicDescription($0)?.pointee.mSampleRate }.first ?? 44100
    let reader = try AVAssetReader(asset: a)
    let duration = seconds > 0 ? CMTime(seconds: seconds, preferredTimescale: 600) : .positiveInfinity
    reader.timeRange = CMTimeRange(start: CMTime(seconds: start, preferredTimescale: 600), duration: duration)
    let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: rate,
                                   AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 32,
                                   AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false,
                                   AVLinearPCMIsBigEndianKey: false]
    let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: settings)
    reader.add(output)
    guard reader.startReading() else { fail("can't read \(path): \(reader.error?.localizedDescription ?? "")") }
    FileManager.default.createFile(atPath: outPath, contents: nil)
    guard let out = FileHandle(forWritingAtPath: outPath) else { fail("can't write \(outPath)") }
    var bytes = 0
    while let buffer = output.copyNextSampleBuffer() {
        guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
        var length = 0
        var pointer: UnsafeMutablePointer<CChar>?
        CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length,
                                    dataPointerOut: &pointer)
        if let pointer, length > 0 {
            out.write(Data(bytes: pointer, count: length))
            bytes += length
        }
    }
    try out.close()
    if reader.status == .failed { fail("can't read \(path): \(reader.error?.localizedDescription ?? "")") }
    print("\(Int(rate)) \(bytes / 4)")
}

let args = CommandLine.arguments
func num(_ i: Int) -> Double { args.count > i ? Double(args[i]) ?? 0 : 0 }
do {
    switch (args.count > 1 ? args[1] : "", args.count) {
    case ("info", 3): try info(args[2])
    case ("frames", 8): try frames(args[2], args[3], num(4), num(5), num(6), Int(num(7)))
    case ("audio", 6): try audio(args[2], args[3], num(4), num(5))
    default: fail("usage: pensieve-media info <file> | frames <file> <dir> <start> <seconds> <fps> <max> | audio <file> <out> <start> <seconds>")
    }
} catch {
    fail("\(args.count > 2 ? args[2] : ""): \(error.localizedDescription)")
}
