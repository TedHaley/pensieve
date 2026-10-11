import Foundation

/// Arithmetic ("2^10", "15% of 240", "sqrt(2)*3") and unit conversions ("3 ft in cm", "72 f to c") typed into the
/// search panel. The answer is one result; opening it copies the number.
enum Calculator {
    struct Answer {
        let value: String
        let copy: String  // the bare number, without grouping or unit
        let detail: String
        let conversion: Bool
    }

    static func answer(_ q: String) -> Answer? {
        let s = q.trimmingCharacters(in: .whitespaces).lowercased()
        guard !s.isEmpty, s.count < 200 else { return nil }
        if let c = convert(s) { return c }
        guard s.contains(where: \.isNumber) || s.contains("pi") || s.contains("π") else { return nil }
        // a date or version, not a sum: 2024-03-15, 3/14/2025, 1.2.3
        if s.wholeMatch(of: #/\d{4}[\-\/.]\d{1,2}([\-\/.]\d{1,2})?|\d{1,2}[\-\/.]\d{1,2}[\-\/.]\d{1,4}/#) != nil { return nil }
        var p = Parser(normalize(s))
        guard let v = p.parse(), p.operations > 0, v.isFinite else { return nil }
        return Answer(value: format(v), copy: plain(v), detail: q.trimmingCharacters(in: .whitespaces), conversion: false)
    }

    static func plain(_ v: Double) -> String {
        v == v.rounded() && abs(v) < 1e15 ? String(Int64(v)) : String(format: "%.10g", v)
    }

    private static func normalize(_ s: String) -> String {
        var t = s.replacingOccurrences(of: "×", with: "*").replacingOccurrences(of: "÷", with: "/")
            .replacingOccurrences(of: "−", with: "-").replacingOccurrences(of: "**", with: "^")
        t = t.replacing(#/(\d),(\d{3})/#) { "\($0.1)\($0.2)" }  // 1,000,000
        t = t.replacing(#/(\d),(\d{3})/#) { "\($0.1)\($0.2)" }
        t = t.replacing(#/([\d.]+)\s*%\s*of\s+/#) { "(\($0.1)/100)*" }  // 15% of 240
        t = t.replacing(#/([\d.]+)\s*([+\-])\s*([\d.]+)\s*%/#) { "\($0.1)*(1\($0.2)\($0.3)/100)" }  // 80 + 15%
        t = t.replacing(#/([\d.]+)\s*%/#) { "(\($0.1)/100)" }
        return t
    }

    static func format(_ v: Double) -> String {
        if v == v.rounded(), abs(v) < 1e15 { return Self.grouped.string(from: NSNumber(value: v)) ?? String(v) }
        if abs(v) >= 1e15 || abs(v) < 1e-6 { return String(format: "%.10g", v) }
        return Self.grouped.string(from: NSNumber(value: Double(String(format: "%.10g", v)) ?? v)) ?? String(v)
    }

    private static let grouped: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 10
        f.usesGroupingSeparator = true
        return f
    }()

    /// Recursive descent over + - * / ^ and !, unary minus, parentheses, implicit products ("2pi", "3(4+5)"),
    /// constants and one-argument functions.
    private struct Parser {
        let chars: [Character]
        var i = 0
        var operations = 0  // a lone number or constant is a search, not a sum

        init(_ s: String) { chars = Array(s) }

        mutating func parse() -> Double? {
            guard let v = expr() else { return nil }
            skip()
            return i == chars.count ? v : nil
        }

        mutating func skip() { while i < chars.count, chars[i] == " " { i += 1 } }

        mutating func peek() -> Character? {
            skip()
            return i < chars.count ? chars[i] : nil
        }

        mutating func expr() -> Double? {
            guard var v = term() else { return nil }
            while let c = peek(), c == "+" || c == "-" {
                i += 1
                guard let r = term() else { return nil }
                v = c == "+" ? v + r : v - r
                operations += 1
            }
            return v
        }

        mutating func term() -> Double? {
            guard var v = unary() else { return nil }
            while let c = peek() {
                if c == "*" || c == "/" {
                    i += 1
                    guard let r = unary() else { return nil }
                    v = c == "*" ? v * r : v / r
                } else if c == "(" || c.isLetter || c == "π" {
                    guard let r = power() else { return nil }
                    v *= r
                } else { break }
                operations += 1
            }
            return v
        }

        /// Minus binds looser than ^, so -3^2 = -9.
        mutating func unary() -> Double? {
            if peek() == "-" {
                i += 1
                return unary().map { -$0 }
            }
            if peek() == "+" { i += 1 }
            return power()
        }

        mutating func power() -> Double? {
            guard let b = postfix() else { return nil }
            guard peek() == "^" else { return b }
            i += 1
            guard let e = unary() else { return nil }  // right-associative: 2^3^2 = 2^9
            operations += 1
            return pow(b, e)
        }

        mutating func postfix() -> Double? {
            guard var v = primary() else { return nil }
            while peek() == "!" {
                i += 1
                guard v >= 0, v <= 170, v == v.rounded() else { return nil }
                v = (1...max(1, Int(v))).reduce(1.0) { $0 * Double($1) }
                operations += 1
            }
            return v
        }

        mutating func primary() -> Double? {
            guard let c = peek() else { return nil }
            if c == "(" {
                i += 1
                let v = expr()
                guard peek() == ")" else { return nil }
                i += 1
                return v
            }
            if c.isNumber || c == "." {
                let start = i
                while i < chars.count, chars[i].isNumber || chars[i] == "." { i += 1 }
                if i < chars.count, chars[i] == "e", i + 1 < chars.count, chars[i + 1].isNumber || chars[i + 1] == "-" {
                    i += 2  // 1e6, 2e-3
                    while i < chars.count, chars[i].isNumber { i += 1 }
                }
                return Double(String(chars[start..<i]))
            }
            if c == "π" {
                i += 1
                return .pi
            }
            guard c.isLetter else { return nil }
            let start = i
            while i < chars.count, chars[i].isLetter || chars[i].isNumber { i += 1 }
            let name = String(chars[start..<i])
            if name == "pi" { return .pi }
            if name == "e" { return M_E }
            guard let f = Self.functions[name], let arg = peek() == "(" ? primary() : power() else { return nil }
            operations += 1
            return f(arg)
        }

        static let functions: [String: (Double) -> Double] = [
            "sqrt": { $0.squareRoot() }, "cbrt": cbrt, "abs": abs, "exp": exp,
            "ln": log, "log": log10, "log2": log2, "log10": log10,
            "sin": sin, "cos": cos, "tan": tan, "asin": asin, "acos": acos, "atan": atan,
            "round": { $0.rounded() }, "floor": floor, "ceil": ceil,
        ]
    }

    private static func convert(_ s: String) -> Answer? {
        guard let m = s.wholeMatch(of: #/(-?[\d.,]+)\s*([a-z0-9²°µ"'\/ ]+?)\s+(?:in|to|as)\s+([a-z0-9²°µ"'\/ ]+)/#)
        else { return nil }
        guard let n = Double(m.1.replacingOccurrences(of: ",", with: "")),
              let from = unit(String(m.2)), let to = unit(String(m.3)), type(of: from) == type(of: to)
        else { return nil }
        let v = Measurement(value: n, unit: from).converted(to: to).value
        guard v.isFinite else { return nil }
        let label = to.symbol
        return Answer(value: "\(format(v)) \(label)", copy: plain(v), detail: "\(format(n)) \(from.symbol) = \(format(v)) \(label)",
                      conversion: true)
    }

    private static func unit(_ name: String) -> Dimension? {
        let k = name.trimmingCharacters(in: .whitespaces)
        if let u = units[k] { return u }
        if k.hasSuffix("s"), let u = units[String(k.dropLast())] { return u }  // plurals: "meters", "cups"
        return nil
    }

    private static let day = UnitDuration(symbol: "d", converter: UnitConverterLinear(coefficient: 86400))
    private static let week = UnitDuration(symbol: "wk", converter: UnitConverterLinear(coefficient: 604800))
    private static let year = UnitDuration(symbol: "yr", converter: UnitConverterLinear(coefficient: 31_557_600))

    private static let units: [String: Dimension] = {
        var u: [String: Dimension] = [:]
        func add(_ d: Dimension, _ names: String...) { for n in names { u[n] = d } }
        add(UnitLength.meters, "m", "meter", "metre")
        add(UnitLength.kilometers, "km", "kilometer", "kilometre")
        add(UnitLength.centimeters, "cm", "centimeter", "centimetre")
        add(UnitLength.millimeters, "mm", "millimeter", "millimetre")
        add(UnitLength.miles, "mi", "mile")
        add(UnitLength.feet, "ft", "foot", "feet", "'")
        add(UnitLength.inches, "in", "inch", "inches", "\"")
        add(UnitLength.yards, "yd", "yard")
        add(UnitLength.nauticalMiles, "nmi", "nautical mile")
        add(UnitMass.kilograms, "kg", "kilo", "kilogram")
        add(UnitMass.grams, "g", "gram")
        add(UnitMass.milligrams, "mg", "milligram")
        add(UnitMass.pounds, "lb", "lbs", "pound")
        add(UnitMass.ounces, "oz", "ounce")
        add(UnitMass.stones, "st", "stone")
        add(UnitMass.metricTons, "t", "tonne", "metric ton")
        add(UnitTemperature.celsius, "c", "°c", "celsius", "degrees c", "degree c")
        add(UnitTemperature.fahrenheit, "f", "°f", "fahrenheit", "degrees f", "degree f")
        add(UnitTemperature.kelvin, "k", "kelvin")
        add(UnitVolume.liters, "l", "liter", "litre")
        add(UnitVolume.milliliters, "ml", "milliliter", "millilitre")
        add(UnitVolume.gallons, "gal", "gallon")
        add(UnitVolume.quarts, "qt", "quart")
        add(UnitVolume.pints, "pt", "pint")
        add(UnitVolume.cups, "cup")
        add(UnitVolume.fluidOunces, "fl oz", "floz", "fluid ounce")
        add(UnitVolume.tablespoons, "tbsp", "tablespoon")
        add(UnitVolume.teaspoons, "tsp", "teaspoon")
        add(UnitSpeed.milesPerHour, "mph", "mi/h", "miles per hour", "mile per hour")
        add(UnitSpeed.kilometersPerHour, "kph", "kmh", "km/h", "kilometers per hour", "kilometres per hour")
        add(UnitSpeed.metersPerSecond, "m/s", "meters per second", "metres per second")
        add(UnitSpeed.knots, "kn", "knot")
        add(UnitDuration.seconds, "sec", "second")
        add(UnitDuration.minutes, "min", "minute")
        add(UnitDuration.hours, "h", "hr", "hour")
        add(day, "day")
        add(week, "wk", "week")
        add(year, "yr", "year")
        add(UnitInformationStorage.bytes, "b", "byte")
        add(UnitInformationStorage.kilobytes, "kb", "kilobyte")
        add(UnitInformationStorage.megabytes, "mb", "megabyte")
        add(UnitInformationStorage.gigabytes, "gb", "gigabyte")
        add(UnitInformationStorage.terabytes, "tb", "terabyte")
        add(UnitArea.squareMeters, "m2", "m²", "sq m", "square meter", "square metre")
        add(UnitArea.squareFeet, "ft2", "ft²", "sq ft", "sqft", "square foot", "square feet")
        add(UnitArea.squareKilometers, "km2", "km²", "sq km", "square kilometer", "square kilometre")
        add(UnitArea.squareMiles, "mi2", "mi²", "sq mi", "square mile")
        add(UnitArea.acres, "acre", "ac")
        add(UnitArea.hectares, "ha", "hectare")
        return u
    }()
}
