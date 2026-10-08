"""3D layout + clustering shared by the session and code maps.

Full UMAP fits are expensive and reshuffle the map, so they only happen when the data has grown
meaningfully; in between, new points are placed next to their nearest neighbours.
"""
import numpy as np


def normalize(P: np.ndarray) -> np.ndarray:
    P = P - np.median(P, axis=0)
    r = np.percentile(np.linalg.norm(P, axis=1), 95) + 1e-9
    return np.clip(P / r, -1.6, 1.6)


def fit3d(X: np.ndarray, n_neighbors=15, min_dist=0.12) -> np.ndarray:
    n = len(X)
    if n < 8:
        from sklearn.decomposition import PCA
        return normalize(PCA(n_components=min(3, n), random_state=0).fit_transform(X)) if n >= 3 else np.zeros((n, 3))
    import umap
    red = umap.UMAP(n_components=3, metric="cosine", n_neighbors=min(n_neighbors, n - 1),
                    min_dist=min_dist, random_state=42, low_memory=True)
    return normalize(red.fit_transform(X))


def place_new(X_known, P_known, X_new, k=4, jitter=0.03):
    """Position new vectors at the similarity-weighted mean of their k nearest placed neighbours."""
    if len(X_known) == 0:
        return np.zeros((len(X_new), 3))
    S = X_new @ X_known.T
    k = min(k, len(X_known))
    idx = np.argpartition(-S, k - 1, axis=1)[:, :k]
    w = np.take_along_axis(S, idx, axis=1).clip(1e-3)
    P = (P_known[idx] * w[..., None]).sum(1) / w.sum(1, keepdims=True)
    return P + np.random.default_rng(0).normal(0, jitter, P.shape)


def cluster(X: np.ndarray, kmin=4, kmax=8):
    """KMeans with k chosen by silhouette; at most 8 so each topic gets a distinct palette slot."""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    n = len(X)
    if n < kmin * 2:
        return np.zeros(n, int)
    best, best_s = None, -1
    for k in range(kmin, min(kmax, n // 3) + 1):
        lab = KMeans(n_clusters=k, n_init=8, random_state=0).fit_predict(X)
        s = silhouette_score(X, lab, metric="cosine")
        if s > best_s:
            best, best_s = lab, s
    # relabel by size so topic 0 is the largest (stable palette order)
    order = np.argsort(-np.bincount(best))
    remap = np.empty_like(order)
    remap[order] = np.arange(len(order))
    return remap[best]


def unit(X):
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
