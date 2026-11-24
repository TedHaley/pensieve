//! The small layers that the text model and both encoders share, written as 0.21's MLX port computes them: norms in
//! float32, then back to the weights' type.

use candle_core::{D, DType, Module, Result, Tensor};
use candle_nn::VarBuilder;

pub const EPS: f64 = 1e-6;

/// A linear layer without bias.
pub fn linear(vb: &VarBuilder, a: usize, b: usize, name: &str) -> Result<candle_nn::Linear> {
    Ok(candle_nn::Linear::new(vb.get((b, a), &format!("{name}.weight"))?, None))
}

/// RMS norm over the last axis, in float32. Without a weight it only scales (Gemma 4's value norm). The weight
/// multiplies as is: no "1 +" as in Gemma 1 to 3.
#[derive(Clone)]
pub struct Rms {
    weight: Option<Tensor>,
}

impl Rms {
    pub fn load(vb: &VarBuilder, dim: usize, name: &str) -> Result<Rms> {
        Ok(Rms { weight: Some(vb.get(dim, &format!("{name}.weight"))?.to_dtype(DType::F32)?) })
    }

    pub fn weight(&self) -> Option<&Tensor> {
        self.weight.as_ref()
    }

    pub fn plain() -> Rms {
        Rms { weight: None }
    }

    pub fn forward(&self, x: &Tensor) -> Result<Tensor> {
        let dt = x.dtype();
        let y = x.to_dtype(DType::F32)?.contiguous()?;
        let y = match &self.weight {
            Some(w) => candle_nn::ops::rms_norm(&y, w, EPS as f32)?,
            None => {
                let ms = y.sqr()?.mean_keepdim(D::Minus1)?;
                y.broadcast_div(&(ms + EPS)?.sqrt()?)?
            }
        };
        y.to_dtype(dt)
    }
}

/// (b, n, heads, d) -> (b, heads, n, d), copied to plain strides. candle counts a transposed tensor with one head
/// as contiguous and keeps its strides, which its Metal matmul then misreads.
pub fn heads_first(t: &Tensor) -> Result<Tensor> {
    let t = t.transpose(1, 2)?.contiguous()?;
    let dims = t.dims().to_vec();
    t.flatten_all()?.reshape(dims)
}

/// `x @ w.T` for a layer that may get a non-contiguous input.
pub fn apply(l: &candle_nn::Linear, x: &Tensor) -> Result<Tensor> {
    l.forward(&x.contiguous()?)
}
