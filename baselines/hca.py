"""Fixed HCA pipeline used by the recorded seed-0..9 evaluation."""
import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
PCA_K,PCA_I,K=4,1,2

def hca(seg, truth, seed, three=False):
    X = StandardScaler().fit_transform(seg)
    n_total = min(PCA_K + PCA_I + 2, X.shape[1], X.shape[0])
    Z = PCA(n_components=n_total, random_state=seed).fit_transform(X)[:, PCA_K - 1:PCA_K - 1 + PCA_I]
    if three:
        lab = KMeans(n_clusters=3, random_state=seed, n_init=10).fit_predict(Z)
        purity = {}
        for c in range(3):
            foll = lab[1:][lab[:-1] == c]
            cnt = np.bincount(foll, minlength=3)
            cnt[c] = 0
            purity[c] = cnt.max() / max(1, len(foll))
        m = max(purity, key=purity.get)
        pred = (lab == m).astype(int)
        n = min(len(pred), len(truth))
        return 100.0 * float(np.mean(pred[:n] == truth[:n]))
    lab = KMeans(n_clusters=K, random_state=seed, n_init=10).fit_predict(Z)
    n = min(len(lab), len(truth))
    acc = 100.0 * float(np.mean(lab[:n] == truth[:n]))
    return max(acc, 100.0 - acc)
