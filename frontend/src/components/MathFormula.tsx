import { useEffect, useRef, useState } from 'react';
import { renderFormula, type MathFormulaInputKind } from './mathJaxRuntime';

interface MathFormulaProps {
  language: MathFormulaInputKind;
  source: string;
  display: boolean;
  fallbackSource: string;
}

type FormulaState = 'loading' | 'rendered' | 'fallback';

export function MathFormula({ language, source, display, fallbackSource }: MathFormulaProps) {
  const mountRef = useRef<HTMLSpanElement>(null);
  const [state, setState] = useState<FormulaState>('loading');

  useEffect(() => {
    let active = true;
    const mount = mountRef.current;
    mount?.replaceChildren();
    setState('loading');

    void renderFormula(source, language, display).then(
      (node) => {
        if (!active || !mount) return;
        mount.replaceChildren(node);
        setState('rendered');
      },
      (error: unknown) => {
        if (!active) return;
        console.warn('Formula conversion failed', {
          language,
          category: error instanceof Error ? error.name : 'unknown',
        });
        setState('fallback');
      },
    );

    return () => {
      active = false;
      mount?.replaceChildren();
    };
  }, [display, language, source]);

  const Tag = display ? 'div' : 'span';
  return (
    <Tag
      className={`math-formula math-formula--${display ? 'display' : 'inline'}`}
      data-formula-language={language}
      data-formula-state={state}
    >
      <span ref={mountRef} className="math-formula__output" />
      {state === 'rendered' ? null : <span className="math-formula__fallback">{fallbackSource}</span>}
    </Tag>
  );
}
