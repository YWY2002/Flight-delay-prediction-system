import { useRef, useState } from 'react';
import './Sparkline.css';

interface SparklineProps {
  points: number[];
  /** CSS color for the line - single series, one hue */
  color: string;
  /** max of the value domain (min is 0) */
  domainMax: number;
  /** label for point i, shown in the hover tooltip */
  labelFor: (index: number, value: number) => string;
  width?: number;
  height?: number;
}

/**
 * Small single-series line for stat tiles: 2px line, soft area fill,
 * crosshair + tooltip on hover. Values stay in text ink; only the mark
 * carries the series color.
 */
export function Sparkline({
  points,
  color,
  domainMax,
  labelFor,
  width = 220,
  height = 56,
}: SparklineProps) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const pad = 4;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  const x = (i: number) => pad + (i / (points.length - 1)) * innerW;
  const y = (v: number) => pad + innerH - (Math.min(v, domainMax) / domainMax) * innerH;

  const linePath = points.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${y(v)}`).join(' ');
  const areaPath = `${linePath} L${x(points.length - 1)},${height - pad} L${x(0)},${height - pad} Z`;

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const px = ((e.clientX - rect.left) / rect.width) * width;
    const i = Math.round(((px - pad) / innerW) * (points.length - 1));
    setHover(Math.max(0, Math.min(points.length - 1, i)));
  };

  return (
    <div className="sparkline">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label={`Trend over the last ${points.length} hours`}
      >
        <path d={areaPath} fill={color} opacity={0.12} />
        <path d={linePath} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" />
        {hover !== null && (
          <g>
            <line
              x1={x(hover)}
              x2={x(hover)}
              y1={pad}
              y2={height - pad}
              stroke="rgba(244, 240, 232, 0.25)"
              strokeWidth={1}
            />
            {/* 8px marker with a 2px surface ring */}
            <circle cx={x(hover)} cy={y(points[hover])} r={5} fill="var(--tower-panel)" />
            <circle cx={x(hover)} cy={y(points[hover])} r={4} fill={color} />
          </g>
        )}
      </svg>
      {hover !== null && (
        <div
          className="sparkline-tip mono"
          style={{ left: `${(x(hover) / width) * 100}%` }}
        >
          {labelFor(hover, points[hover])}
        </div>
      )}
    </div>
  );
}
