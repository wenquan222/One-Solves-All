"""Numerical search implementation exported from the recorded experiment."""
import argparse,itertools,json,random,sys,time
from pathlib import Path
import numpy as np
from sklearn.cluster import DBSCAN,OPTICS,AgglomerativeClustering,KMeans,SpectralClustering
from sklearn.decomposition import PCA,FastICA,KernelPCA
from sklearn.manifold import TSNE
from sklearn.mixture import GaussianMixture
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler
ROOT=Path(__file__).resolve().parents[1]
KMEANS_NINIT="auto"
SEED_DIM=False
SEED_VALUES=list(range(10))
STOCHASTIC_RED={"TSNE","UMAP"}
STOCHASTIC_CLU={"KMeans","GMM"}
STANDARDIZE=[False,True]
def make_reducers():
    pca_dims = [2, 1, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 30, 50]
    gammas = [None, 0.001, 0.01, 0.1, 1.0, 10.0]
    kpca = []
    for k in ('rbf', 'poly', 'sigmoid', 'cosine', 'linear'):
        for n in (2, 3, 5):
            if k in ('cosine', 'linear'):
                kpca.append(dict(kernel=k, n_components=n, gamma=None))
            elif k == 'poly':
                kpca += [dict(kernel=k, n_components=n, gamma=g, degree=d) for g in gammas for d in (3, 2, 4)]
            else:
                kpca += [dict(kernel=k, n_components=n, gamma=g) for g in gammas]
    tsne = [dict(perplexity=p) for p in (30, 5, 10, 15, 50, 100)]
    umap_ = [dict(n_neighbors=k, min_dist=d) for k in (15, 5, 10, 30, 50, 100) for d in (0.1, 0.0, 0.25, 0.5, 0.8)]
    return [('PCA', dict(n_components=n)) for n in pca_dims] + [('TSNE', c) for c in tsne] + [('UMAP', c) for c in umap_] + [('KernelPCA', c) for c in kpca]

def make_clusterers():
    return [('KMeans', {})] + [('DBSCAN', dict(q=q, min_samples=m)) for q in (0.9, 0.7, 0.5, 0.8, 0.95) for m in (5, 10, 20)] + [('GMM', dict(covariance_type=c)) for c in ('full', 'diag', 'spherical', 'tied')] + [('Agglomerative', dict(linkage=l)) for l in ('ward', 'average', 'complete', 'single')]

REDUCERS=make_reducers()
CLUSTERERS=make_clusterers()

def all_configs():
    out = []
    for (r, rk), (c, ck), s in itertools.product(REDUCERS, CLUSTERERS, STANDARDIZE):
        base = dict(std=s, red=r, red_kw=rk, clu=c, clu_kw=ck)
        if SEED_DIM and (r in STOCHASTIC_RED or c in STOCHASTIC_CLU):
            out += [dict(base, rs=v) for v in SEED_VALUES]
        else:
            out.append(base)
    return out

class Runner:

    def __init__(self, segments, n_d, truth, seed, constraints='sm'):
        self.seg = segments
        self.seg_std = StandardScaler().fit_transform(segments)
        self.constraints = constraints
        self.k = 3 if constraints == 'fpga3' else 2
        self.n_d = n_d
        self.truth = truth
        self.seed = seed
        self.cache = {}

    def embed(self, std, red, kw, rs):
        key = (std, red, json.dumps(kw, sort_keys=True), rs)
        if key in self.cache:
            return self.cache[key]
        X = self.seg_std if std else self.seg
        if red == 'PCA':
            Z = PCA(n_components=kw['n_components'], random_state=rs).fit_transform(X)
        elif red == 'KernelPCA':
            Z = KernelPCA(n_components=kw['n_components'], kernel=kw['kernel'], gamma=kw.get('gamma'), degree=kw.get('degree', 3), random_state=rs).fit_transform(X)
        elif red == 'TSNE':
            Z = TSNE(n_components=2, perplexity=kw['perplexity'], init='pca', random_state=rs).fit_transform(X)
        elif red == 'UMAP':
            import umap
            Z = umap.UMAP(n_components=2, n_neighbors=kw['n_neighbors'], min_dist=kw['min_dist'], random_state=rs).fit_transform(X)
        elif red == 'FastICA':
            Z = FastICA(n_components=kw['n_components'], random_state=rs).fit_transform(X)
        else:
            raise ValueError(red)
        self.cache[key] = Z
        return Z

    def cluster(self, Z, clu, kw, rs):
        if clu == 'KMeans':
            return KMeans(n_clusters=self.k, n_init=KMEANS_NINIT, random_state=rs).fit_predict(Z)
        if clu == 'GMM':
            return GaussianMixture(n_components=self.k, covariance_type=kw['covariance_type'], random_state=rs).fit_predict(Z)
        if clu == 'Agglomerative':
            return AgglomerativeClustering(n_clusters=self.k, linkage=kw['linkage']).fit_predict(Z)
        if clu == 'DBSCAN':
            m = kw['min_samples']
            d = NearestNeighbors(n_neighbors=m).fit(Z).kneighbors(Z)[0][:, -1]
            eps = float(np.quantile(d, kw['q'])) or 1e-09
            return self._k_clusters(Z, DBSCAN(eps=eps, min_samples=m).fit_predict(Z), self.k)
        if clu == 'OPTICS':
            return self._k_clusters(Z, OPTICS(min_samples=kw['min_samples'], xi=kw['xi']).fit_predict(Z), self.k)
        if clu == 'Spectral':
            extra = dict(gamma=kw['gamma']) if kw['affinity'] == 'rbf' else dict(n_neighbors=kw['n_neighbors'])
            return SpectralClustering(n_clusters=self.k, affinity=kw['affinity'], random_state=rs, **extra).fit_predict(Z)
        raise ValueError(clu)

    @staticmethod
    def _k_clusters(Z, lab, k=2):
        """密度聚类（DBSCAN / OPTICS）：必须正好 k 类，噪声点归到最近的簇中心。"""
        ids = [c for c in np.unique(lab) if c != -1]
        if len(ids) != k:
            return None
        if np.any(lab == -1):
            cents = np.stack([Z[lab == c].mean(0) for c in ids])
            noise = lab == -1
            lab[noise] = np.array(ids)[np.argmin(((Z[noise][:, None] - cents[None]) ** 2).sum(-1), 1)]
        return np.searchsorted(ids, lab)

    @staticmethod
    def label_fpga3(lab):
        """FPGA（选择输入 N−1）三类定标签。只用提示词给出的序列结构，不看真值：
          S2（1 的平方）是唯一会紧跟在同类后面的类型 → 自身相邻次数最多的类记 S2；
          另外两类成对出现，M 在前、S1 在后 → 更常排在对方前面的记 M，另一类记 S1。
        返回每段的类型 'M' / 'S1' / 'S2'。"""
        trans = np.zeros((3, 3), int)
        np.add.at(trans, (lab[:-1], lab[1:]), 1)
        s2 = int(np.argmax(np.diag(trans)))
        x, y = [c for c in range(3) if c != s2]
        m, s1 = (x, y) if trans[x, y] >= trans[y, x] else (y, x)
        names = {m: 'M', s1: 'S1', s2: 'S2'}
        return [names[int(l)] for l in lab]

    def attempt(self, cfg):
        t0 = time.time()
        err = None
        rs = cfg.get('rs', self.seed)
        try:
            Z = self.embed(cfg['std'], cfg['red'], cfg['red_kw'], rs)
            if not np.all(np.isfinite(Z)):
                raise FloatingPointError('embedding has nan/inf')
            lab = self.cluster(Z, cfg['clu'], cfg['clu_kw'], rs)
        except Exception as e:
            lab, err = (None, f'{type(e).__name__}: {e}'[:120])
        dt = time.time() - t0
        if lab is None:
            return dict(cfg=cfg, valid=False, passed=False, violation=10 ** 9, acc=None, n_d=None, sec=dt, error=err)
        extra = {}
        if self.constraints == 'fpga3':
            typ = self.label_fpga3(lab)
            seq = ''.join(('A' if t == 'M' else 'D' for t in typ))
            n_m_bad = sum((1 for i, t in enumerate(typ) if t == 'M' and (i + 1 == len(typ) or typ[i + 1] != 'S1')))
            n_s1_bad = sum((1 for i, t in enumerate(typ) if t == 'S1' and (i == 0 or typ[i - 1] != 'M')))
            extra = dict(n_m_bad=n_m_bad, n_s1_bad=n_s1_bad)
        else:
            big = np.argmax(np.bincount(lab, minlength=2))
            seq = ''.join(('D' if l == big else 'A' for l in lab))
        nd = seq.count('D')
        n_aa = sum((1 for i in range(1, len(seq)) if seq[i] == 'A' and seq[i - 1] == 'A'))
        first_bad = int(seq[0] != 'D')
        if self.constraints == 'none':
            violation = 0
        else:
            violation = abs(nd - self.n_d) + n_aa + first_bad + sum(extra.values())
        n = min(len(seq), len(self.truth))
        acc = 100.0 * sum((seq[i] == self.truth[i] for i in range(n))) / n
        return dict(cfg=cfg, valid=True, passed=violation == 0, violation=violation, n_d=nd, n_aa=n_aa, first_bad=first_bad, acc=acc, sec=dt, **extra)
