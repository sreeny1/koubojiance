"""违禁词检测器。

双层匹配策略（对文本与词库做同一套归一化，保证一致性）：
1. 归一化精确层：全角→半角、繁→简、小写、去除空白与标点符号后做子串匹配。
   可命中"第 一 名"（分词规避）、"最·好"（符号插入）等变体。
2. 拼音谐音层：把文本与词都转为无声调拼音序列，按"字级 token"匹配。
   可命中"蕞好"→"最好"、"低架"→"低价"等谐音替换。

命中后通过归一化位置 → 原文字符位置的映射，反查原文片段，
并按字符位置在字幕段时间段内线性内插出毫秒级时间，供一键定位。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from pypinyin import Style, lazy_pinyin
from zhconv import convert as zh_convert

from .database import Database, get_db

# 归一化时丢弃的字符类别：标点(P*)、符号(S*)、空白/分隔(Z*)、控制/格式(Cc/Cf)
_DROP_CATS_PREFIX = ("P", "S", "Z")
_DROP_CATS_EXACT = ("Cc", "Cf")


def _keep_char(ch: str) -> bool:
    cat = unicodedata.category(ch)
    if cat.startswith(_DROP_CATS_PREFIX) or cat in _DROP_CATS_EXACT:
        return False
    return True


def normalize(text: str) -> tuple[str, list[int]]:
    """归一化文本。

    返回 (归一化字符串, 位置映射表)——映射表第 k 项是归一化串第 k 个
    字符在原文中的下标，用于反查原文片段与高亮位置。
    """
    norm_chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        # NFKC：全角→半角等；可能展开为多字符（罕见），逐个处理并都映射到 i
        for c in unicodedata.normalize("NFKC", ch):
            c = zh_convert(c, "zh-hans").lower()
            if _keep_char(c):
                norm_chars.append(c)
                index_map.append(i)
    return "".join(norm_chars), index_map


def _char_pinyin_tokens(norm: str) -> list[str] | None:
    """归一化字符串 → 字级拼音 token 列表。

    汉字取无声调拼音；非汉字（字母/数字）原样保留一个 token。
    """
    if not norm:
        return None
    tokens: list[str] = []
    for c in norm:
        if "\u4e00" <= c <= "\u9fff":
            py = lazy_pinyin(c, style=Style.NORMAL, errors="ignore")
            tokens.append(py[0] if py else c)
        else:
            tokens.append(c)
    return tokens


class Detector:
    """违禁词检测器（持有当前词库的内存索引，词库变更后调用 reload）。"""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db or get_db()
        self.reload()

    def reload(self) -> None:
        """从数据库重新加载启用的词库。"""
        rows = self._db.query(
            """
            SELECT w.id, w.word, c.id AS category_id, c.name AS category_name,
                   c.color AS category_color
            FROM words w JOIN categories c ON w.category_id = c.id
            WHERE w.enabled = 1
            """
        )
        self.words: list[dict[str, Any]] = []
        for r in rows:
            norm, _ = normalize(r["word"])
            if not norm:
                continue  # 词库项归一化后为空（如纯标点），跳过
            tokens = _char_pinyin_tokens(norm)
            self.words.append({
                **r,
                "_norm": norm,
                "_tokens": tokens,
            })

    # ------------------------------------------------------------------
    @staticmethod
    def _interp(seg_start_ms: int, seg_end_ms: int, pos: int, total: int) -> int:
        """按字符位置在字幕段内线性内插时间（近似定位用）。"""
        if total <= 1:
            return seg_start_ms
        ratio = pos / (total - 1)
        return int(seg_start_ms + ratio * (seg_end_ms - seg_start_ms))

    def scan_text(
        self, text: str, seg_start_ms: int, seg_end_ms: int, segment_id: int
    ) -> list[dict[str, Any]]:
        """扫描一条字幕文本，返回命中列表（未入库）。"""
        hits: list[dict[str, Any]] = []
        if not text or not self.words:
            return hits

        norm, idx_map = normalize(text)
        if not norm:
            return hits
        text_tokens = _char_pinyin_tokens(norm)

        # 构造拼音 token 串（以空格分隔、首尾带空格），并记录每个 token
        # 的起始位置 → 归一化字符下标 的映射（token 与字一一对应）
        token_str = ""
        token_start: dict[int, int] = {}
        if text_tokens:
            parts: list[str] = [" "]
            for i, t in enumerate(text_tokens):
                token_start[len("".join(parts))] = i
                parts.append(t)
                parts.append(" ")
            token_str = "".join(parts)

        seen_spans: set[tuple[int, int, int]] = set()

        for w in self.words:
            wnorm = w["_norm"]
            # ---- 第一层：归一化子串匹配 ----
            start = norm.find(wnorm)
            while start != -1:
                end = start + len(wnorm)
                self._add_hit(
                    hits, seen_spans, w, text, idx_map, norm, start, end,
                    seg_start_ms, seg_end_ms, segment_id,
                )
                start = norm.find(wnorm, start + 1)

            # ---- 第二层：拼音 token 匹配（谐音变体）----
            wtok = w["_tokens"]
            if not wtok or not token_str:
                continue
            pattern = " " + " ".join(wtok) + " "
            for m in re.finditer(re.escape(pattern), token_str):
                # pattern 以空格开头，m.start(0)+1 恰为命中 token 的起始位置；
                # token 序号即命中词在归一化串中的起始下标
                tok_idx = token_start.get(m.start(0) + 1)
                if tok_idx is None:
                    continue
                n_start = tok_idx
                n_end = n_start + len(wnorm)
                self._add_hit(
                    hits, seen_spans, w, text, idx_map, norm, n_start, n_end,
                    seg_start_ms, seg_end_ms, segment_id,
                )

        return hits

    def _add_hit(
        self, hits, seen, w, text, idx_map, norm, n_start, n_end,
        seg_start_ms, seg_end_ms, segment_id,
    ) -> None:
        key = (w["id"], n_start, n_end)
        if key in seen:
            return
        if n_end > len(idx_map):
            return
        o_start = idx_map[n_start]
        # 结束映射：取命中最后一个字符在原文中的下标 +1
        o_end = idx_map[n_end - 1] + 1
        if o_end <= o_start:
            return
        seen.add(key)
        hits.append({
            "segment_id": segment_id,
            "word_id": w["id"],
            "word_text": w["word"],
            "matched_text": text[o_start:o_end],
            "char_start": o_start,
            "char_end": o_end,
            "start_ms": self._interp(seg_start_ms, seg_end_ms, o_start, len(text)),
            "end_ms": self._interp(seg_start_ms, seg_end_ms, o_end - 1, len(text)),
            "sentence": text,
            "category_id": w["category_id"],
            "category_name": w["category_name"],
            "category_color": w["category_color"],
        })

    # ------------------------------------------------------------------
    def scan_video(self, video_id: int) -> int:
        """对单个视频的全部字幕重新检测（替换旧结果），返回命中数。"""
        segments = self._db.query(
            "SELECT id, start_ms, end_ms, text FROM segments "
            "WHERE video_id=? ORDER BY idx", (video_id,)
        )
        rows = []
        for s in segments:
            for h in self.scan_text(s["text"], s["start_ms"], s["end_ms"], s["id"]):
                h.pop("char_start", None)
                h.pop("char_end", None)
                rows.append((
                    video_id, h["segment_id"], h["word_id"], h["word_text"],
                    h["matched_text"], h["start_ms"], h["end_ms"], h["sentence"],
                ))
        with self._db.tx() as conn:
            conn.execute("DELETE FROM hits WHERE video_id=?", (video_id,))
            if rows:
                conn.executemany(
                    "INSERT INTO hits(video_id,segment_id,word_id,word_text,"
                    "matched_text,start_ms,end_ms,sentence) "
                    "VALUES(?,?,?,?,?,?,?,?)", rows,
                )
        return len(rows)

    def scan_all(self) -> dict[str, int]:
        """对所有已转写视频重新检测（换词库后无需重新转写）。"""
        videos = self._db.query(
            "SELECT DISTINCT video_id v FROM segments"
        )
        total = 0
        for v in videos:
            total += self.scan_video(v["v"])
        return {"videos": len(videos), "hits": total}
