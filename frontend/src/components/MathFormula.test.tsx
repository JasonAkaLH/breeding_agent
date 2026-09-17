import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MathFormula } from './MathFormula';
import { renderFormula } from './mathJaxRuntime';

vi.mock('./mathJaxRuntime', () => ({
  renderFormula: vi.fn(),
}));

const mockedRenderFormula = vi.mocked(renderFormula);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

describe('MathFormula', () => {
  beforeEach(() => {
    mockedRenderFormula.mockReset();
  });

  it('shows source immediately and mounts the converted node on success', async () => {
    const pending = deferred<HTMLElement>();
    mockedRenderFormula.mockReturnValueOnce(pending.promise);
    const { container } = render(
      <MathFormula language="tex" source="x^2" fallbackSource="$x^2$" display={false} />,
    );

    const formula = container.querySelector('.math-formula') as HTMLElement;
    expect(formula).toHaveAttribute('data-formula-state', 'loading');
    expect(formula).toHaveTextContent('$x^2$');
    expect(mockedRenderFormula).toHaveBeenCalledWith('x^2', 'tex', false);

    const output = document.createElement('mjx-container');
    output.dataset.testFormula = 'x-squared';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('aria-hidden', 'true');
    const assistiveMath = document.createElement('mjx-assistive-mml');
    assistiveMath.textContent = 'x squared';
    output.append(svg, assistiveMath);
    pending.resolve(output);

    await waitFor(() => expect(formula).toHaveAttribute('data-formula-state', 'rendered'));
    expect(container.querySelector('[data-test-formula="x-squared"]')).toBe(output);
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('mjx-assistive-mml')).toHaveTextContent('x squared');
    expect(container.querySelector('[tabindex]')).not.toBeInTheDocument();
    expect(screen.queryByText('$x^2$')).not.toBeInTheDocument();
  });

  it('keeps readable source and reports only bounded metadata when conversion fails', async () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    mockedRenderFormula.mockRejectedValueOnce(new Error('contains-secret-formula-source'));
    const { container } = render(
      <MathFormula language="mathml" source="<math><mi>x</mi></math>" fallbackSource="<math><mi>x</mi></math>" display />,
    );

    const formula = container.querySelector('.math-formula') as HTMLElement;
    await waitFor(() => expect(formula).toHaveAttribute('data-formula-state', 'fallback'));
    expect(formula).toHaveTextContent('<math><mi>x</mi></math>');
    expect(formula).toHaveClass('math-formula--display');
    expect(warning).toHaveBeenCalledWith('Formula conversion failed', { language: 'mathml', category: 'Error' });
    expect(JSON.stringify(warning.mock.calls)).not.toContain('contains-secret-formula-source');
    warning.mockRestore();
  });

  it('discards stale conversion results after streamed source changes', async () => {
    const first = deferred<HTMLElement>();
    const second = deferred<HTMLElement>();
    mockedRenderFormula.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { container, rerender } = render(
      <MathFormula language="tex" source="old" fallbackSource="$old$" display={false} />,
    );

    rerender(<MathFormula language="tex" source="new" fallbackSource="$new$" display={false} />);
    expect(container).toHaveTextContent('$new$');

    const currentNode = document.createElement('mjx-container');
    currentNode.dataset.testFormula = 'new';
    second.resolve(currentNode);
    await waitFor(() => expect(container.querySelector('[data-test-formula="new"]')).toBe(currentNode));

    const staleNode = document.createElement('mjx-container');
    staleNode.dataset.testFormula = 'old';
    first.resolve(staleNode);
    await Promise.resolve();
    expect(container.querySelector('[data-test-formula="old"]')).not.toBeInTheDocument();
    expect(container.querySelector('[data-test-formula="new"]')).toBe(currentNode);
  });

  it('does not mount a pending result after unmount', async () => {
    const pending = deferred<HTMLElement>();
    mockedRenderFormula.mockReturnValueOnce(pending.promise);
    const { container, unmount } = render(
      <MathFormula language="tex" source="x" fallbackSource="$x$" display={false} />,
    );

    unmount();
    const output = document.createElement('mjx-container');
    pending.resolve(output);
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
    expect(output.isConnected).toBe(false);
  });
});
