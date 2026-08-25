import { describe, expect, it } from 'vitest';
import { withoutSetValue } from './collections';

describe('withoutSetValue', () => {
  it('removes the requested value without mutating the current Set', () => {
    const current = new Set(['keep', 'remove']);

    const next = withoutSetValue(current, 'remove');

    expect([...current]).toEqual(['keep', 'remove']);
    expect([...next]).toEqual(['keep']);
    expect(next).not.toBe(current);
  });

  it('returns a new Set when the value is absent', () => {
    const current = new Set(['keep']);

    const next = withoutSetValue(current, 'missing');

    expect([...next]).toEqual(['keep']);
    expect(next).not.toBe(current);
  });
});
