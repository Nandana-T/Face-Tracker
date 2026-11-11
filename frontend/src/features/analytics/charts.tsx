import { motion } from "motion/react";
import { useId } from "react";

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
}

export interface ChartPoint {
  /** Machine label, also used in the accessible data table. */
  label: string;
  /** Short axis text. */
  tick: string;
  values: Record<string, number>;
}

interface Props {
  title: string;
  points: ChartPoint[];
  series: ChartSeries[];
  stacked?: boolean;
  testId?: string;
}

const W = 640;
const H = 220;
const PAD = { l: 36, r: 8, t: 8, b: 26 };

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / pow) * pow;
}

/**
 * Small, dependency-free SVG bar chart. Values are exactly the numbers the API returned: no smoothing,
 * no interpolation and no count-up animation (bars simply fade in, so no intermediate "count" is ever
 * suggested). Every chart also renders its data as a visually hidden table for assistive technology.
 */
export function BarChart({ title, points, series, stacked = false, testId }: Props) {
  const id = useId();
  const totals = points.map((p) =>
    stacked ? series.reduce((a, s) => a + (p.values[s.key] ?? 0), 0) : Math.max(...series.map((s) => p.values[s.key] ?? 0), 0),
  );
  const max = niceMax(Math.max(...totals, 0));
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const slot = plotW / Math.max(points.length, 1);
  const barW = Math.max(2, Math.min(36, slot * 0.7) / (stacked ? 1 : series.length));
  const y = (v: number) => PAD.t + plotH - (v / max) * plotH;
  const every = Math.max(1, Math.ceil(points.length / 8));

  return (
    <figure data-testid={testId} className="space-y-2">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${id}-t`} className="h-56 w-full">
        <title id={`${id}-t`}>{title}</title>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(max * f)} y2={y(max * f)} className="stroke-border" strokeDasharray={f === 0 ? undefined : "3 4"} />
            <text x={PAD.l - 6} y={y(max * f) + 4} textAnchor="end" className="fill-muted-foreground text-[10px]">
              {Math.round(max * f)}
            </text>
          </g>
        ))}
        {points.map((p, i) => {
          const x0 = PAD.l + slot * i + (slot - barW * (stacked ? 1 : series.length)) / 2;
          let acc = 0;
          return (
            <motion.g key={p.label} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.25 }}>
              {series.map((s, k) => {
                const v = p.values[s.key] ?? 0;
                const top = stacked ? y(acc + v) : y(v);
                const h = stacked ? y(acc) - y(acc + v) : y(0) - y(v);
                const x = stacked ? x0 : x0 + k * barW;
                acc += v;
                return v > 0 ? (
                  <rect key={s.key} x={x} y={top} width={barW - 1} height={Math.max(h, 1)} rx={2} fill={s.color} data-testid={`bar-${s.key}`}>
                    <title>{`${p.label}: ${s.label} ${v}`}</title>
                  </rect>
                ) : null;
              })}
              {i % every === 0 ? (
                <text x={PAD.l + slot * i + slot / 2} y={H - 8} textAnchor="middle" className="fill-muted-foreground text-[10px]">
                  {p.tick}
                </text>
              ) : null}
            </motion.g>
          );
        })}
      </svg>
      <figcaption className="flex flex-wrap gap-3 text-xs text-muted-foreground">
        {series.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm" style={{ background: s.color }} aria-hidden />
            {s.label}
          </span>
        ))}
      </figcaption>
      <table className="sr-only" data-testid={testId ? `${testId}-table` : undefined}>
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>Period (UTC)</th>
            {series.map((s) => (
              <th key={s.key}>{s.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.label}>
              <th scope="row">{p.label}</th>
              {series.map((s) => (
                <td key={s.key}>{p.values[s.key] ?? 0}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/** Two-part proportion bar (e.g. new vs returning identities). */
export function ProportionBar({ parts, testId }: { parts: { label: string; value: number; color: string }[]; testId?: string }) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  return (
    <div data-testid={testId} className="space-y-2">
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-muted" role="img" aria-label={parts.map((p) => `${p.label} ${p.value}`).join(", ")}>
        {parts.map((p) =>
          p.value > 0 ? (
            <motion.div key={p.label} initial={{ opacity: 0 }} animate={{ opacity: 1 }} style={{ width: `${(p.value / total) * 100}%`, background: p.color }} />
          ) : null,
        )}
      </div>
      <ul className="flex flex-wrap gap-4 text-xs">
        {parts.map((p) => (
          <li key={p.label} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm" style={{ background: p.color }} aria-hidden />
            <span className="text-muted-foreground">{p.label}</span>
            <span className="font-mono font-semibold tabular-nums" data-testid={`part-${p.label.toLowerCase().replace(/\W+/g, "-")}`}>
              {p.value}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
