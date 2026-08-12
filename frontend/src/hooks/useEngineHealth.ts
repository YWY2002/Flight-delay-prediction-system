import { useCallback, useEffect, useRef, useState } from 'react';

export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

export type PulseState =
  | { kind: 'checking' }
  | { kind: 'awake'; latencyMs: number; checkedAt: Date }
  | { kind: 'asleep'; checkedAt: Date };

/** Polls the backend /health endpoint. Shared by the public pulse drawer
 *  and the admin console. */
export function useEngineHealth(pollMs = 30_000): { pulse: PulseState; check: () => void } {
  const [pulse, setPulse] = useState<PulseState>({ kind: 'checking' });
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const check = useCallback(async () => {
    const started = performance.now();
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 4000);
      const res = await fetch(`${API_BASE}/health`, { signal: controller.signal });
      clearTimeout(timeout);
      if (res.ok) {
        setPulse({
          kind: 'awake',
          latencyMs: Math.round(performance.now() - started),
          checkedAt: new Date(),
        });
        return;
      }
      setPulse({ kind: 'asleep', checkedAt: new Date() });
    } catch {
      setPulse({ kind: 'asleep', checkedAt: new Date() });
    }
  }, []);

  useEffect(() => {
    void check();
    timer.current = setInterval(() => void check(), pollMs);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [check, pollMs]);

  return { pulse, check: () => void check() };
}
