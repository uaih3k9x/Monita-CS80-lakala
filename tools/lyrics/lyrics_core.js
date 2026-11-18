// 歌词页纯函数（node --test tools/lyrics/tests/ 覆盖），不碰 DOM/canvas。浏览器原型与 mac/ 里 LyricsCore.swift 行为一致。

/** 进度插值：数据源给的是 at（毫秒）时刻的 pos（秒），按 rate 外推到 nowMs。 */
export function positionAt(d, nowMs) {
  if (!d || d.pos == null) return 0;
  const rate = d.state === 'playing' ? d.rate ?? 1 : 0;
  let p = d.pos + ((nowMs - (d.at ?? nowMs)) / 1000) * rate;
  if (p < 0) p = 0;
  if (d.duration && p > d.duration) p = d.duration;
  return p;
}

/** 正在唱的行：最后一个 t <= pos 的下标；前奏返回 -1。 */
export function lineIndex(lines, pos) {
  let lo = 0;
  let hi = lines ? lines.length : 0;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid].t <= pos) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

export function fmtTime(s) {
  if (s == null || !isFinite(s)) return '-:--';
  s = Math.floor(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** [上一句, 当前句, 下一句, 下下句]，越界为 null。 */
export function around(lines, idx) {
  const at = (i) => (i >= 0 && i < lines.length ? lines[i] : null);
  return [at(idx - 1), at(idx), at(idx + 1), at(idx + 2)];
}

/** 切句滚动进度 0→1（changedAt 为切句时刻）。 */
export function scrollPhase(changedAt, nowMs, dur) {
  if (changedAt == null) return 1;
  return Math.max(0, Math.min(1, (nowMs - changedAt) / dur));
}

/** 折行：measure(s) 返回像素宽；CJK 逐字断，拉丁按空格断；超出 maxLines 末行加 …。 */
export function wrapText(measure, text, maxW, maxLines) {
  const tokens = String(text ?? '').match(/[A-Za-z0-9'’\-.,!?]+\s*|\s+|./gu) || [];
  const lines = [];
  let cur = '';
  for (const tok of tokens) {
    const next = cur + tok;
    if (measure(next.trimEnd()) <= maxW || !cur) {
      cur = next;
      // 单个 token 本身超宽：硬切
      while (measure(cur.trimEnd()) > maxW && cur.length > 1) {
        let k = cur.length - 1;
        while (k > 1 && measure(cur.slice(0, k)) > maxW) k--;
        lines.push(cur.slice(0, k));
        cur = cur.slice(k);
      }
    } else {
      lines.push(cur.trimEnd());
      cur = tok.trimStart();
    }
  }
  lines.push(cur.trimEnd());
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    let last = kept[maxLines - 1];
    while (last.length && measure(last + '…') > maxW) last = last.slice(0, -1);
    kept[maxLines - 1] = last + '…';
    return kept;
  }
  return lines;
}
