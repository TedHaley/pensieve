//! Tests on the real model. They pass without running when the model is not in the Hugging Face cache.

use embed::{Embedder, Kind};

fn model() -> Option<Embedder> {
    let folder = embed::files::model_folder(embed::MODEL)?;
    embed::files::part_file(&folder, embed::Part::Text)?;
    Embedder::load(&folder, &[]).ok()
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

#[test]
fn the_embedder_can_be_shared_between_threads() {
    fn shared<T: Send + Sync>() {}
    shared::<Embedder>();
}

#[test]
fn a_text_gets_the_same_vector_alone_and_in_a_batch() {
    let Some(e) = model() else { return };
    let texts = ["a longer text about sorting a list of numbers in place, with a few more words", "short", "fn add(a: i32, b: i32) -> i32 { a + b }"];
    let all = e.embed_texts(&texts, Kind::Document(None)).unwrap();
    for (i, t) in texts.iter().enumerate() {
        let one = e.embed_texts(&[t], Kind::Document(None)).unwrap();
        assert!(dot(&one[0], &all[i]) > 0.9999, "text {i}");
    }
    assert_eq!(all[0].len(), e.dim());
    assert!((dot(&all[0], &all[0]) - 1.0).abs() < 1e-4);
}

#[test]
fn the_fast_path_gives_candles_vectors() {
    let Some(fast) = model() else { return };
    let long = "a long text that goes on ".repeat(60);
    let texts = ["fn add(a: i32, b: i32) -> i32 { a + b }", "short", long.as_str()];
    let a = fast.embed_texts(&texts, Kind::Document(None)).unwrap();
    let slow = fast.without_fast();
    let b = slow.embed_texts(&texts, Kind::Document(None)).unwrap();
    for i in 0..texts.len() {
        assert!(dot(&a[i], &b[i]) > 0.99999, "text {i}: {}", dot(&a[i], &b[i]));
    }
}

#[test]
fn a_query_is_nearer_the_text_that_answers_it() {
    let Some(e) = model() else { return };
    let q = e.embed_texts(&["how do I sort a list in place"], Kind::Query).unwrap();
    let d = e.embed_texts(&["Call list.sort() to sort a list in place.", "The weather is sunny and warm today."],
                          Kind::Document(None)).unwrap();
    assert!(dot(&q[0], &d[0]) > dot(&q[0], &d[1]));
}

#[test]
fn a_long_text_is_cut_at_512_tokens() {
    let Some(e) = model() else { return };
    let long = "word ".repeat(2000);
    let longer = format!("{long} and then a different ending that comes after the cut");
    let v = e.embed_texts(&[&long, &longer], Kind::Raw).unwrap();
    assert!(dot(&v[0], &v[1]) > 0.9999);
}
