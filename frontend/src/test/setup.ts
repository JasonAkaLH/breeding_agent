import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(window, 'ResizeObserver', {
  writable: true,
  value: ResizeObserverMock,
});

const localStore = new Map<string, string>();
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem: vi.fn((key: string) => localStore.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { localStore.set(key, String(value)); }),
    removeItem: vi.fn((key: string) => { localStore.delete(key); }),
    clear: vi.fn(() => { localStore.clear(); }),
  },
});
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: window.localStorage,
});

Object.defineProperty(globalThis, 'crypto', {
  configurable: true,
  value: {
    ...globalThis.crypto,
    randomUUID: vi.fn(() => `uuid-${Math.random().toString(16).slice(2)}`),
  },
});

const originalGetComputedStyle = window.getComputedStyle.bind(window);
Object.defineProperty(window, 'getComputedStyle', {
  configurable: true,
  value: (element: Element) => originalGetComputedStyle(element),
});
