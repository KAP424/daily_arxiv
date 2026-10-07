"""TF-IDF + 余弦相似度 —— 用 numpy 自己算, 顶掉 sklearn 和 scipy。

为什么不用 sklearn 了
--------------------------------------------------------------------------
本程序对 sklearn 的用法只有三处, 全是 ``TfidfVectorizer`` +
``cosine_similarity`` (rank.py / analyze.py / profile.py)。就为这三处, 单文件 exe
要多背 **35.8 MB** (实测压缩后: scipy 20.4 + scipy 自己那份 OpenBLAS 9.5 +
sklearn 5.9), 295 个归档条目, 每次启动还要多解 105 MB 到临时目录 —— 而 scipy
那份 OpenBLAS 和 numpy 那份是重复的。换掉之后 exe **74.0 → 38.0 MB**, 启动
**3.35 → 1.72 秒** (同一目录交替跑的端到端实测)。

数字必须和 sklearn 对得上
--------------------------------------------------------------------------
"自己写个 TF-IDF" 最怕的是**悄悄算出不一样的分数**: 推荐排序变了, 界面上却一切
正常, 没人看得出来。所以这个文件不是"实现一个差不多的 TF-IDF", 而是照着
sklearn 1.3.2 的源码逐句复刻:

* 分词就是 ``re.findall(r"(?u)\\b\\w\\w+\\b", doc.lower())`` —— sklearn 的默认
  ``token_pattern`` + ``lowercase=True``, 没有 ``strip_accents``。
* 停用词在**组 n-gram 之前**滤掉 (``_word_ngrams`` 先过滤再拼), 所以双词短语里
  不会出现停用词; 顺序也是先全部单词、再全部双词。
* 词表最终按**字母序**编号, ``get_feature_names_out()`` 就是这个顺序。
  (sklearn 是"先出现先编号, 最后 ``_sort_features`` 重排", 最终结果一样。)
* ``min_df`` / ``max_df`` 按文档频率剪; ``max_features`` 按**词频和**取前 N 个,
  并列时的取舍用和 sklearn 一模一样的 ``(-tfs[mask]).argsort()[:limit]``,
  连 ``tfs`` 的 dtype 都跟着用 int64 —— dtype 变了 argsort 的并列顺序可能变。
* ``sublinear_tf`` 是 ``log(tf) + 1``; idf 是 ``log((n+1)/(df+1)) + 1``
  (``smooth_idf`` 的默认行为)。
* 每行 L2 归一,**全零行保持全零**不除零 (sklearn 只在非零元上除)。
* 稀疏矩阵和 scipy 一样"每行内列号升序", 求和顺序也一致, 这样点积的浮点尾数
  能逐位对上 (实测 ``np.bincount`` 和 ``csr.sum(axis=0)`` 逐位相同, 见
  selftest 第 37 节)。

selftest 第 37 节拿**真实的文献库**把这里和 sklearn 逐项比: 词表、矩阵非零元、
余弦相似度、以及三处调用点的最终结果。**改这个文件之后一定要跑它。**

没实现的参数
--------------------------------------------------------------------------
只支持 ``analyzer="word"``、``norm`` 为 ``"l2"`` 或 ``None``、``use_idf`` /
``smooth_idf`` / ``lowercase`` 这几个; 别的组合直接抛 ``NotImplementedError``,
免得"悄悄用了不一样的参数"却没人发现。
"""

from __future__ import annotations

import re
from numbers import Integral
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

# sklearn 的默认分词正则。注意 ``\w`` 在 unicode 下也匹配中文和带重音的字母,
# 所以 "Bogoliubov" 这类词会被原样留下, 而单字符的词 (如 "a") 会被丢掉 ——
# ``\w\w+`` 要求至少两个字符。
_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")

# sklearn 的 ``ENGLISH_STOP_WORDS``, 318 个词, 一字不差地抄过来。
# (它是 frozenset, 顺序无所谓; 这里按字母序排只是为了看着整齐。)
ENGLISH_STOP_WORDS = frozenset("""
a about above across after afterwards again against all almost alone along
already also although always am among amongst amoungst amount an and
another any anyhow anyone anything anyway anywhere are around as at back
be became because become becomes becoming been before beforehand behind
being below beside besides between beyond bill both bottom but by call can
cannot cant co con could couldnt cry de describe detail do done down due
during each eg eight either eleven else elsewhere empty enough etc even
ever every everyone everything everywhere except few fifteen fifty fill
find fire first five for former formerly forty found four from front full
further get give go had has hasnt have he hence her here hereafter hereby
herein hereupon hers herself him himself his how however hundred i ie if
in inc indeed interest into is it its itself keep last latter latterly
least less ltd made many may me meanwhile might mill mine more moreover
most mostly move much must my myself name namely neither never
nevertheless next nine no nobody none noone nor not nothing now nowhere of
off often on once one only onto or other others otherwise our ours
ourselves out over own part per perhaps please put rather re same see seem
seemed seeming seems serious several she should show side since sincere
six sixty so some somehow someone something sometime sometimes somewhere
still such system take ten than that the their them themselves then thence
there thereafter thereby therefore therein thereupon these they thick thin
third this those though three through throughout thru thus to together too
top toward towards twelve twenty two un under until up upon us very via
was we well were what whatever when whence whenever where whereafter
whereas whereby wherein whereupon wherever whether which while whither who
whoever whole whom whose why will with within without would yet you your
yours yourself yourselves
""".split())


# --------------------------------------------------------------------------
# 最小稀疏矩阵
# --------------------------------------------------------------------------
class _ColSum:
    """按列求和的结果。

    scipy 的 ``csr.sum(axis=0)`` 返回 ``np.matrix``, 调用点取它的 ``.A1`` 拿到一维
    数组 (profile.py 里就是 ``matrix.sum(axis=0).A1``)。这里照抄这个接口, 省得
    改调用点。
    """

    __slots__ = ("_v",)

    def __init__(self, v: np.ndarray) -> None:
        self._v = v

    @property
    def A1(self) -> np.ndarray:
        return self._v

    def __array__(self, dtype: Any = None) -> np.ndarray:
        return np.asarray(self._v, dtype=dtype)


class _CsrMatrix:
    """最小 CSR 矩阵: 只实现本程序用到的那几个操作。

    整个 scipy 就为了这几个方法被背进 exe 里, 不划算。这里三个 numpy 数组
    (``data`` / ``indices`` / ``indptr``) 拼一个, 够用。

    约定: ``indices`` 在**每一行内按列号升序** (和 scipy 的 ``sort_indices()``
    之后一样), 这样求和顺序才和 scipy 一致。
    """

    __slots__ = ("data", "indices", "indptr", "shape")

    def __init__(self, data: np.ndarray, indices: np.ndarray,
                 indptr: np.ndarray, shape: Tuple[int, int]) -> None:
        self.data = data            # 非零元 (float64 或 int64)
        self.indices = indices      # 列号, 每行内升序
        self.indptr = indptr        # 每行非零元的起止下标
        self.shape = shape

    def __repr__(self) -> str:
        return "<稀疏矩阵 %d×%d, 非零元 %d>" % (
            self.shape[0], self.shape[1], self.data.size)

    @property
    def nnz(self) -> int:
        """非零元个数 (scipy 的 ``csr_matrix.nnz`` 同名同义)。"""
        return int(self.data.size)

    # --- 取用 ---
    def __getitem__(self, key: Union[int, slice]) -> "_CsrMatrix":
        """按行取: ``M[i]`` 取一行, ``M[a:b]`` 取连续几行。

        调用点用到的是 ``matrix[0:1]`` 和 ``matrix[1:]`` (rank.py 把画像那行和
        候选那几行分开)。
        """
        if isinstance(key, slice):
            start, stop, step = key.indices(self.shape[0])
            if step != 1:
                raise IndexError("只支持连续的行切片: %r" % (key,))
            lo, hi = int(self.indptr[start]), int(self.indptr[stop])
            # 减掉 lo 是为了让子矩阵的 indptr 从 0 开始
            return _CsrMatrix(self.data[lo:hi], self.indices[lo:hi],
                              self.indptr[start:stop + 1] - lo,
                              (stop - start, self.shape[1]))
        if isinstance(key, (int, np.integer)):
            i = int(key)
            if i < 0:
                i += self.shape[0]
            if not 0 <= i < self.shape[0]:
                raise IndexError(key)
            lo, hi = int(self.indptr[i]), int(self.indptr[i + 1])
            return _CsrMatrix(self.data[lo:hi], self.indices[lo:hi],
                              np.array([0, hi - lo], dtype=np.int64),
                              (1, self.shape[1]))
        raise TypeError("不支持的下标: %r" % (key,))

    def sum(self, axis: int = 0) -> _ColSum:
        """按列求和。``np.bincount`` 的累加顺序和 scipy 逐位相同 (实测)。"""
        if axis != 0:
            raise NotImplementedError("只实现了按列求和 (axis=0)")
        return _ColSum(np.bincount(self.indices, weights=self.data,
                                   minlength=self.shape[1]))

    def toarray(self) -> np.ndarray:
        """铺成稠密数组。只在调试和测试里用。"""
        out = np.zeros(self.shape, dtype=np.float64)
        for i in range(self.shape[0]):
            lo, hi = int(self.indptr[i]), int(self.indptr[i + 1])
            out[i, self.indices[lo:hi]] = self.data[lo:hi]
        return out

    def _select_columns(self, kept: np.ndarray) -> "_CsrMatrix":
        """只留下 ``kept`` 这几列 (升序), 列号重新编号。

        ``kept`` 升序 + 行内列号本来就升序 ⇒ 留下来的非零元在行内仍然升序。
        """
        remap = np.full(self.shape[1], -1, dtype=np.int64)
        remap[kept] = np.arange(kept.size, dtype=np.int64)
        new_cols = remap[self.indices]
        keep = new_cols >= 0
        if keep.all():
            return _CsrMatrix(self.data, new_cols.astype(np.int32),
                              self.indptr, (self.shape[0], kept.size))
        rows = np.repeat(np.arange(self.shape[0]), np.diff(self.indptr))[keep]
        counts = np.bincount(rows, minlength=self.shape[0])
        indptr = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
        return _CsrMatrix(self.data[keep], new_cols[keep].astype(np.int32),
                          indptr, (self.shape[0], kept.size))


def _reduce_rows(values: np.ndarray, indptr: np.ndarray,
                 n_rows: int) -> np.ndarray:
    """把 ``values`` (CSR 的 data, 已按行排好) 按行求和, 空行留在 0。

    这是本模块唯一一处"按行累加", 归一化的行范数和余弦的点积都走这里, 免得两处
    各写一遍又各错一遍。

    ``reduceat`` 只对**非空行**调用, 两个原因:

    1. 它遇到"起止下标相等"时会返回该位置那一个元素而不是 0 —— 空行会拿到下一
       行的第一个数 (一个凭空冒出来的相似度!);
    2. 更要命的是, 想把空行的起点挪到别处来绕过第 1 点是不行的 —— ``reduceat``
       是拿 ``indices[i+1]`` 当第 i 段的**结束**边界的, 改了空行的起点就等于把
       它**上一行**的累加范围截断了, 那一行会只剩一个元素。

    所以照 scipy ``_minor_reduce`` 的做法: 只挑出非空行来 reduceat, 再散落回
    全零数组。非空行的起点必然 < len(values), 顺带把"结尾全是空行"时的越界也
    躲开了。
    """
    out = np.zeros(n_rows, dtype=np.float64)
    if values.size == 0:
        return out
    rows = np.flatnonzero(np.diff(indptr))
    if rows.size == 0:
        return out
    out[rows] = np.add.reduceat(values, indptr[rows])
    return out


def _row_sums_of_squares(data: np.ndarray, indptr: np.ndarray,
                         n_rows: int) -> np.ndarray:
    """每行的平方和, 空行给 0。

    累加顺序按列号升序, 和 sklearn 的 ``inplace_csr_row_normalize_l2`` 同一个
    顺序 (见 ``_l2_normalize_rows``), 也和 scipy 的 ``_minor_reduce`` 一样 ——
    所以 ``A.multiply(A).sum(axis=1)`` 能对上。
    """
    return _reduce_rows(data * data, indptr, n_rows)


def _l2_normalize_rows(M: _CsrMatrix) -> _CsrMatrix:
    """每行 L2 归一 (返回新的, 不改原矩阵)。

    全零行保持全零 —— 和 sklearn 的 ``inplace_csr_row_normalize_l2`` 一致。

    这里**乘** 1/norm 而不是除以 norm, 是照抄 sklearn 的写法:

    .. code-block:: cython

        sum_ = 0.0
        for j in range(X_indptr[i], X_indptr[i + 1]):
            sum_ += X_data[j] * X_data[j]
        if sum_ == 0.0:
            continue                      # 全零行不动
        sq_norm = 1.0 / sqrt(sum_)
        for j in ...:
            X_data[j] *= sq_norm

    ``a / b`` 和 ``a * (1.0 / b)`` 在浮点下不是一回事 (差 1 个 ulp), 想和 sklearn
    对得上就得用后者。
    """
    norms = np.sqrt(_row_sums_of_squares(M.data, M.indptr, M.shape[0]))
    reps = np.diff(M.indptr)
    if norms.size:
        # 范数为 0 的只有全零行 (剪枝之后整篇文档一个词不剩是会发生的)。sklearn
        # 碰到 0 范数就跳过那一行, 这里把 0 换成 1 —— 空行没有非零元, 乘 1 和
        # 跳过是一回事, 但省得 1/0 冒出一个 inf 的 RuntimeWarning 糊在日志里。
        norms = np.where(norms == 0, 1.0, norms)
    data = M.data * np.repeat(1.0 / norms, reps)
    return _CsrMatrix(data, M.indices, M.indptr, M.shape)


def cosine_similarity(X: _CsrMatrix, Y: Optional[_CsrMatrix] = None) -> np.ndarray:
    """余弦相似度, 返回稠密二维数组 ``(X 的行数, Y 的行数)``。

    和 sklearn 一样**先把两边都重新归一化一遍** (哪怕传进来的已经是归一化过的
    tf-idf 矩阵) —— 这不是多余的: 归一化两次和一次在浮点尾数上不一样, 想和
    sklearn 逐位对上就得跟着做。

    点积按"每行内列号升序逐个累加"算, 和 scipy 的稀疏矩阵乘法同顺序。
    """
    if Y is None:
        Y = X
    Xn = _l2_normalize_rows(X)
    Yn = Xn if Y is X else _l2_normalize_rows(Y)
    if Xn.shape[1] != Yn.shape[1]:
        raise ValueError("两个矩阵的列数不一样: %d vs %d"
                         % (Xn.shape[1], Yn.shape[1]))

    out = np.zeros((Xn.shape[0], Yn.shape[0]), dtype=np.float64)
    for i in range(Xn.shape[0]):
        lo, hi = int(Xn.indptr[i]), int(Xn.indptr[i + 1])
        if lo == hi:
            continue                      # 这一行全零, 相似度全是 0
        q = np.zeros(Yn.shape[1], dtype=np.float64)
        q[Xn.indices[lo:hi]] = Xn.data[lo:hi]
        # 稠密行 × 稀疏矩阵: 只要 Y 每行内列号升序, 累加顺序就和 scipy 一样
        out[i] = _reduce_rows(q[Yn.indices] * Yn.data, Yn.indptr, Yn.shape[0])
    return out


# --------------------------------------------------------------------------
# 向量化
# --------------------------------------------------------------------------
class TfidfVectorizer:
    """sklearn ``TfidfVectorizer`` 的替身。

    参数名和默认值都跟 sklearn 一致, 但**只实现本程序用到的部分**; 用法也只有
    ``fit_transform`` / ``transform`` / ``get_feature_names_out`` 这三个。

    这里没有 ``Counter`` 之类的花活, 就是照着 sklearn 的步骤走一遍, 好读、好对。
    """

    def __init__(self, stop_words: Union[str, Sequence[str], None] = None,
                 ngram_range: Tuple[int, int] = (1, 1),
                 sublinear_tf: bool = False,
                 min_df: Union[int, float] = 1,
                 max_df: Union[int, float] = 1.0,
                 max_features: Optional[int] = None,
                 use_idf: bool = True,
                 smooth_idf: bool = True,
                 norm: Optional[str] = "l2",
                 lowercase: bool = True,
                 token_pattern: str = r"(?u)\b\w\w+\b") -> None:
        if norm not in ("l2", None):
            raise NotImplementedError("只实现了 norm='l2' 或 None, 收到 %r" % (norm,))
        if not use_idf and not sublinear_tf:
            # 纯词频也没什么不能算的, 只是本程序用不到, 不给自己留没测过的路
            raise NotImplementedError("use_idf=False 且 sublinear_tf=False 没实现")
        if isinstance(stop_words, str):
            if stop_words != "english":
                raise ValueError("不是内置停用词表: %s" % stop_words)
            stop_words = ENGLISH_STOP_WORDS
        elif stop_words is not None:
            stop_words = frozenset(stop_words)

        self.ngram_range = tuple(ngram_range)
        self.sublinear_tf = sublinear_tf
        self.min_df = min_df
        self.max_df = max_df
        self.max_features = max_features
        self.use_idf = use_idf
        self.smooth_idf = smooth_idf
        self.norm = norm
        self.lowercase = lowercase
        self._stop_words = stop_words
        self._token_re = re.compile(token_pattern)

        self.vocabulary_: Optional[Dict[str, int]] = None
        self._names: Optional[np.ndarray] = None     # 下标 -> 词
        self._idf: Optional[np.ndarray] = None

    # --- 分词 (sklearn 的 build_analyzer / _word_ngrams) ---
    def _tokens(self, doc: str) -> List[str]:
        """预处理 → 分词 → 滤停用词 → 拼 n-gram, 一步不差地照 sklearn 来。"""
        text = doc.lower() if self.lowercase else doc
        tokens = self._token_re.findall(text)
        if self._stop_words is not None:
            # 先滤停用词再拼短语: 所以 "the model" 这种双词不会出现
            tokens = [t for t in tokens if t not in self._stop_words]

        min_n, max_n = self.ngram_range
        if max_n == 1:
            return tokens
        # 顺序: 先所有单词, 再所有双词 —— 和 sklearn 一样
        out = list(tokens) if min_n == 1 else []
        start = 2 if min_n == 1 else min_n
        n_tokens = len(tokens)
        for n in range(start, min(max_n + 1, n_tokens + 1)):
            for i in range(n_tokens - n + 1):
                out.append(" ".join(tokens[i:i + n]))
        return out

    def _term_counts(self, doc: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for term in self._tokens(doc):
            counts[term] = counts.get(term, 0) + 1
        return counts

    @staticmethod
    def _counts_to_csr(per_doc: List[Dict[str, int]],
                       vocabulary: Dict[str, int]) -> _CsrMatrix:
        """按词表编号铺成 CSR。行内按列号升序 (= sklearn 的 sort_indices)。"""
        indices: List[int] = []
        data: List[int] = []
        indptr: List[int] = [0]
        for counts in per_doc:
            for term, n in counts.items():
                idx = vocabulary.get(term)
                if idx is not None:
                    indices.append(idx)
                    data.append(n)
            # 一行内按列号排序 —— 求和顺序要和 scipy 一致, 这步不能省
            order = sorted(range(indptr[-1], len(indices)),
                           key=indices.__getitem__)
            indices[indptr[-1]:] = [indices[i] for i in order]
            data[indptr[-1]:] = [data[i] for i in order]
            indptr.append(len(indices))
        return _CsrMatrix(np.asarray(data, dtype=np.int64),
                          np.asarray(indices, dtype=np.int32),
                          np.asarray(indptr, dtype=np.int64),
                          (len(per_doc), len(vocabulary)))

    # --- 主流程 ---
    def fit_transform(self, raw_documents: Iterable[str]) -> _CsrMatrix:
        """学词表 + 算 idf, 返回 tf-idf 矩阵。"""
        docs = list(raw_documents)
        per_doc = [self._term_counts(d) for d in docs]

        terms = set()
        for counts in per_doc:
            terms.update(counts)
        if not terms:
            raise ValueError("empty vocabulary; perhaps the documents only "
                             "contain stop words")

        # 按字母序编号。sklearn 是"先出现先编号"再按名字重排, 最终顺序相同;
        # 而 max_features 并列时的取舍也是按字母序 (它排序在前、剪枝在后),
        # 所以这里从一开始就排好, 两边等价。
        names = np.asarray(sorted(terms), dtype=object)
        vocabulary = {term: i for i, term in enumerate(names)}
        X = self._counts_to_csr(per_doc, vocabulary)

        kept = self._prune(X)
        names = names[kept]
        X = X._select_columns(kept)
        self.vocabulary_ = {str(term): i for i, term in enumerate(names)}
        self._names = names
        return self._tfidf(X, fit=True)

    def transform(self, raw_documents: Iterable[str]) -> _CsrMatrix:
        """用已经学好的词表和 idf 处理新文档。"""
        if self.vocabulary_ is None:
            raise ValueError("这个 TfidfVectorizer 还没 fit 过")
        per_doc = [self._term_counts(d) for d in raw_documents]
        X = self._counts_to_csr(per_doc, self.vocabulary_)
        return self._tfidf(X, fit=False)

    def fit(self, raw_documents: Iterable[str]) -> "TfidfVectorizer":
        self.fit_transform(raw_documents)
        return self

    def get_feature_names_out(self) -> np.ndarray:
        if self._names is None:
            raise ValueError("这个 TfidfVectorizer 还没 fit 过")
        return self._names.copy()

    # --- 剪枝 (sklearn 的 _limit_features) ---
    def _prune(self, X: _CsrMatrix) -> np.ndarray:
        """按 min_df / max_df / max_features 砍词, 返回留下的列号 (升序)。

        只看矩阵里的 df / tf, 不碰词表 —— 词表的编号是列号, 砍完由调用方重排。
        """
        n_doc = X.shape[0]
        n_features = X.shape[1]
        dfs = np.bincount(X.indices, minlength=n_features)

        min_df, max_df = self.min_df, self.max_df
        max_doc_count = max_df if isinstance(max_df, Integral) else max_df * n_doc
        min_doc_count = min_df if isinstance(min_df, Integral) else min_df * n_doc
        if max_doc_count < min_doc_count:
            raise ValueError("max_df 对应的文档数比 min_df 还少")

        mask = np.ones(n_features, dtype=bool)
        mask &= dfs <= max_doc_count
        mask &= dfs >= min_doc_count

        if self.max_features is not None and mask.sum() > self.max_features:
            # 词频和。sklearn 是在 int32 的计数矩阵上求和 (得 int64), 这里跟着
            # 用 int64 —— 并列时 argsort 的取舍只跟比较结果有关, 但 dtype 别变
            # 是最省心的。
            tfs = np.bincount(X.indices, weights=X.data,
                              minlength=n_features).astype(np.int64)
            # 这个表达式是从 sklearn 抄的, 一个字都别改
            mask_inds = (-tfs[mask]).argsort()[:self.max_features]
            new_mask = np.zeros(n_features, dtype=bool)
            new_mask[np.where(mask)[0][mask_inds]] = True
            mask = new_mask

        kept = np.where(mask)[0]
        if kept.size == 0:
            raise ValueError("剪完之后一个词都不剩了 (min_df 调小点, 或 max_df "
                             "调大点)")
        return kept

    # --- 算 tf-idf (sklearn 的 TfidfTransformer) ---
    def _tfidf(self, X: _CsrMatrix, fit: bool) -> _CsrMatrix:
        data = X.data.astype(np.float64)
        n_doc, n_features = X.shape

        if self.sublinear_tf:
            data = np.log(data) + 1.0

        if self.use_idf:
            if fit:
                # df 只看"这一列在几篇文档里出现过", 和 tf 的缩放无关, 所以
                # 在 sublinear_tf 之后算也一样。
                df = np.bincount(X.indices, minlength=n_features).astype(np.float64)
                df += 1.0                                   # smooth_idf
                self._idf = np.log((n_doc + 1.0) / df) + 1.0
            data = data * self._idf[X.indices]

        out = _CsrMatrix(data, X.indices, X.indptr, X.shape)
        if self.norm == "l2":
            out = _l2_normalize_rows(out)
        return out
