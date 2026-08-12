import { useMemo } from 'react';
import './SkyBackground.css';

/**
 * Fixed full-viewport twilight sky: layered gradient, slow drifting clouds,
 * a field of faint stars, and the occasional tiny aircraft crossing.
 * Pure CSS animation so it costs almost nothing.
 */
export function SkyBackground() {
  const stars = useMemo(
    () =>
      Array.from({ length: 70 }, (_, i) => ({
        id: i,
        left: Math.random() * 100,
        top: Math.random() * 55,
        size: Math.random() * 1.6 + 0.6,
        delay: Math.random() * 6,
      })),
    [],
  );

  return (
    <div className="sky" aria-hidden="true">
      <div className="sky-gradient" />
      {stars.map((s) => (
        <span
          key={s.id}
          className="sky-star"
          style={{
            left: `${s.left}%`,
            top: `${s.top}%`,
            width: s.size,
            height: s.size,
            animationDelay: `${s.delay}s`,
          }}
        />
      ))}
      <div className="sky-cloud sky-cloud-a" />
      <div className="sky-cloud sky-cloud-b" />
      <div className="sky-cloud sky-cloud-c" />
      <div className="sky-plane">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
          <path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z" />
        </svg>
      </div>
    </div>
  );
}
