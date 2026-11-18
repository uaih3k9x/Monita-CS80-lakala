// 歌词页前端纯函数测试：node --test tools/lyrics/tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { positionAt, lineIndex, fmtTime, around, scrollPhase, wrapText } from '../lyrics_core.js';

test('positionAt 播放中按 rate 外推', () => {
  const d = { state: 'playing', pos: 10, at: 1000_000, rate: 1, duration: 200 };
  assert.equal(positionAt(d, 1003_500), 13.5);
});

test('positionAt 暂停不动、超出时长截断、负数归零', () => {
  assert.equal(positionAt({ state: 'paused', pos: 10, at: 0, rate: 1 }, 99_000), 10);
  assert.equal(positionAt({ state: 'playing', pos: 199, at: 0, rate: 1, duration: 200 }, 50_000), 200);
  assert.equal(positionAt({ state: 'playing', pos: 0, at: 10_000, rate: 1 }, 5_000), 0);
  assert.equal(positionAt(null, 1), 0);
});

test('lineIndex 二分', () => {
  const lines = [{ t: 1 }, { t: 5 }, { t: 9 }];
  assert.equal(lineIndex(lines, 0.2), -1);
  assert.equal(lineIndex(lines, 1), 0);
  assert.equal(lineIndex(lines, 8.99), 1);
  assert.equal(lineIndex(lines, 100), 2);
  assert.equal(lineIndex([], 3), -1);
});

test('fmtTime', () => {
  assert.equal(fmtTime(0), '0:00');
  assert.equal(fmtTime(65.9), '1:05');
  assert.equal(fmtTime(3600 + 61), '1:01:01');
  assert.equal(fmtTime(null), '-:--');
});

test('around 取上一句/当前/后两句，越界给 null', () => {
  const lines = ['a', 'b', 'c', 'd'].map((x, i) => ({ t: i, x }));
  assert.deepEqual(around(lines, 1).map((l) => l && l.x), ['a', 'b', 'c', 'd']);
  assert.deepEqual(around(lines, 0).map((l) => l && l.x), [null, 'a', 'b', 'c']);
  assert.deepEqual(around(lines, 3).map((l) => l && l.x), ['c', 'd', null, null]);
  // 前奏：当前句为空，下一句是第一句
  assert.deepEqual(around(lines, -1).map((l) => l && l.x), [null, null, 'a', 'b']);
});

test('scrollPhase 切句后 0→1，超时为 1', () => {
  assert.equal(scrollPhase(1000, 1000, 400), 0);
  assert.equal(scrollPhase(1000, 1200, 400), 0.5);
  assert.equal(scrollPhase(1000, 9999, 400), 1);
  assert.equal(scrollPhase(null, 5, 400), 1);
});

test('wrapText 按宽度折行，超出行数加省略号', () => {
  const measure = (s) => s.length * 10; // 每字 10px
  assert.deepEqual(wrapText(measure, '一二三四五六', 30, 3), ['一二三', '四五六']);
  assert.deepEqual(wrapText(measure, '一二三四五六七', 30, 2), ['一二三', '四五…']);
  // 英文尽量按词断
  assert.deepEqual(wrapText(measure, 'ab cd ef', 50, 3), ['ab cd', 'ef']);
  assert.deepEqual(wrapText(measure, '', 50, 2), ['']);
});
