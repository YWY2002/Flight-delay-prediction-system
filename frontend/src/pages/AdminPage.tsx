import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { PasswordInput, TextInput, Tooltip } from '@mantine/core';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  CircleOff,
  LogOut,
  MoonStar,
  Radar,
  RefreshCcw,
  ShieldAlert,
  Timer,
  TowerControl,
  TrendingUp,
} from 'lucide-react';
import { PortholeClock } from '../components/PortholeClock';
import { Sparkline } from '../components/Sparkline';
import { API_BASE, useEngineHealth } from '../hooks/useEngineHealth';
import {
  EVENT_KIND_LABEL,
  MODEL,
  POLLERS,
  delaySeries,
  makeEvent,
  seedEvents,
  type EventKind,
  type OpsEvent,
  type PollerStatus,
} from '../data/admin';
import { AIRPORTS, type AirportCode } from '../data/flights';
import './AdminPage.css';

const AUTH_KEY = 'halcyon-tower-auth';

/* ------------------------------------------------------------------ */
/* Login                                                               */
/* ------------------------------------------------------------------ */

function TowerLogin({ onSuccess }: { onSuccess: () => void }) {
  const [user, setUser] = useState('');
  const [pass, setPass] = useState('');
  const [failed, setFailed] = useState(false);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (user.trim() === 'admin' && pass === 'tower') {
      sessionStorage.setItem(AUTH_KEY, '1');
      onSuccess();
    } else {
      setFailed(true);
    }
  };

  return (
    <div className="tower tower-login-wrap">
      <motion.form
        className="tower-login"
        onSubmit={submit}
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div className="tower-login-head">
          <span className="tower-beacon" aria-hidden="true" />
          <div>
            <p className="eyebrow">Restricted · operations</p>
            <h1 className="display tower-login-title">Halcyon Tower</h1>
          </div>
        </div>

        <TextInput
          label="Callsign"
          value={user}
          onChange={(e) => {
            setUser(e.currentTarget.value);
            setFailed(false);
          }}
          placeholder="admin"
          autoFocus
          radius="md"
        />
        <PasswordInput
          label="Clearance"
          value={pass}
          onChange={(e) => {
            setPass(e.currentTarget.value);
            setFailed(false);
          }}
          placeholder="••••••"
          radius="md"
        />

        {failed && (
          <p className="tower-login-error" role="alert">
            <ShieldAlert size={14} /> That callsign and clearance don't match. Try again.
          </p>
        )}

        <button type="submit" className="tower-login-submit">
          Enter the tower
        </button>

        <p className="tower-login-hint">
          Demo access: <span className="mono">admin</span> / <span className="mono">tower</span> -
          real authentication arrives with the backend.
        </p>

        <Link to="/" className="tower-login-back">
          ← Back to the traveler page
        </Link>
      </motion.form>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Dashboard pieces                                                    */
/* ------------------------------------------------------------------ */

const STATUS_META: Record<
  PollerStatus,
  { label: string; className: string; Icon: typeof CheckCircle2 }
> = {
  healthy: { label: 'Healthy', className: 'ok', Icon: CheckCircle2 },
  degraded: { label: 'Degraded', className: 'warn', Icon: AlertTriangle },
  stalled: { label: 'Stalled', className: 'bad', Icon: CircleOff },
};

const EVENT_ICON: Record<EventKind, typeof RefreshCcw> = {
  'go-around': RefreshCcw,
  hold: Timer,
  program: ShieldAlert,
  cascade: TrendingUp,
};

function DelayTile({ airport }: { airport: AirportCode }) {
  const series = useMemo(() => delaySeries(airport), [airport]);
  const current = series[series.length - 1];
  const threeHoursAgo = series[series.length - 4];
  const delta = current - threeHoursAgo;
  const rising = delta > 0;

  const hourLabel = (i: number, v: number) => {
    const h = (new Date().getHours() - (series.length - 1 - i) + 24) % 24;
    return `${String(h).padStart(2, '0')}:00 · ${v}`;
  };

  return (
    <div className="tower-panel tower-tile">
      <div className="tower-tile-top">
        <div>
          <p className="eyebrow">{AIRPORTS[airport].iata} · delay index</p>
          <p className="tower-tile-number mono">{current}</p>
        </div>
        <span className={`tower-delta ${rising ? 'warn' : 'ok'}`}>
          {rising ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
          {rising ? '+' : ''}
          {delta} vs 3h ago
        </span>
      </div>
      <Sparkline
        points={series}
        color="var(--calm-sky)"
        domainMax={100}
        labelFor={hourLabel}
      />
      <p className="tower-tile-foot">last 24 h · hourly</p>
    </div>
  );
}

function PollerPanel({ now }: { now: number }) {
  return (
    <section className="tower-panel tower-pollers" aria-label="Ingestion pollers">
      <header className="tower-panel-head">
        <h2 className="display">Ingestion</h2>
        <p>Four sources, each on its own cadence</p>
      </header>
      <ul className="tower-poller-list">
        {POLLERS.map((p) => {
          const { label, className, Icon } = STATUS_META[p.status];
          const ago = (p.lastPollOffsetSec + Math.floor(now / 1000)) % p.cadenceSec;
          return (
            <li key={p.id} className="tower-poller">
              <span className={`tower-status ${className}`}>
                <Icon size={15} />
                {label}
              </span>
              <div className="tower-poller-name">
                <span>{p.name}</span>
                <em>{p.note ?? p.detail}</em>
              </div>
              <span className="mono tower-poller-cadence">{p.cadence}</span>
              <Tooltip label={`${p.rowsToday.toLocaleString()} rows today`} position="top">
                <span className="mono tower-poller-ago">{ago}s ago</span>
              </Tooltip>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function EngineTile() {
  const { pulse, check } = useEngineHealth();
  return (
    <section className="tower-panel tower-engine" aria-label="Serving API">
      <header className="tower-panel-head">
        <h2 className="display">Serving API</h2>
        <p className="mono tower-endpoint">{API_BASE}/health</p>
      </header>
      {pulse.kind === 'awake' ? (
        <p className="tower-status ok tower-engine-state">
          <Activity size={16} /> Awake · {pulse.latencyMs} ms
        </p>
      ) : pulse.kind === 'asleep' ? (
        <p className="tower-status idle tower-engine-state">
          <MoonStar size={16} /> Not reachable - console running on preview data
        </p>
      ) : (
        <p className="tower-status idle tower-engine-state">Checking…</p>
      )}
      <button type="button" className="tower-ghost-btn" onClick={check}>
        Ping now
      </button>
    </section>
  );
}

function ModelCard() {
  return (
    <section className="tower-panel tower-model" aria-label="Model">
      <header className="tower-panel-head">
        <h2 className="display">Model</h2>
        <p className="mono">{MODEL.version}</p>
      </header>
      <dl className="tower-model-facts">
        <div>
          <dt>Horizon</dt>
          <dd>{MODEL.horizon}</dd>
        </div>
        <div>
          <dt>AUC-PR</dt>
          <dd className="mono">{MODEL.aucPr.toFixed(2)}</dd>
        </div>
        <div>
          <dt>Brier</dt>
          <dd className="mono">{MODEL.brier.toFixed(2)}</dd>
        </div>
        <div>
          <dt>Features</dt>
          <dd className="mono">{MODEL.features}</dd>
        </div>
        <div>
          <dt>Trained</dt>
          <dd>
            {MODEL.trainedOn} · {MODEL.trainingWindow}
          </dd>
        </div>
      </dl>
      <p className="tower-model-note">{MODEL.calibrationNote}</p>
    </section>
  );
}

function EventFeed({ events }: { events: OpsEvent[] }) {
  return (
    <section className="tower-panel tower-events" aria-label="Approach anomalies">
      <header className="tower-panel-head">
        <h2 className="display">Approach anomalies</h2>
        <p>Go-arounds, holds, and flow programs as the detector sees them</p>
      </header>
      <ul className="tower-event-list">
        <AnimatePresence initial={false}>
          {events.map((ev) => {
            const Icon = EVENT_ICON[ev.kind];
            return (
              <motion.li
                key={ev.id}
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.25 }}
                className="tower-event"
              >
                <span className="mono tower-event-time">
                  {ev.time.toLocaleTimeString('en-US', {
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false,
                  })}
                </span>
                <span className="tower-event-kind">
                  <Icon size={14} />
                  {EVENT_KIND_LABEL[ev.kind]}
                </span>
                <span className="mono tower-event-airport">{AIRPORTS[ev.airport].iata}</span>
                <span className="tower-event-detail">{ev.detail}</span>
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ul>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Dashboard                                                           */
/* ------------------------------------------------------------------ */

function TowerDashboard({ onSignOut }: { onSignOut: () => void }) {
  const [elapsed, setElapsed] = useState(0);
  const [events, setEvents] = useState<OpsEvent[]>(seedEvents);

  useEffect(() => {
    const tick = setInterval(() => setElapsed((e) => e + 1000), 1000);
    const feed = setInterval(() => {
      setEvents((prev) => [makeEvent(0), ...prev].slice(0, 9));
    }, 26_000);
    return () => {
      clearInterval(tick);
      clearInterval(feed);
    };
  }, []);

  return (
    <div className="tower">
      <header className="tower-header">
        <div className="tower-wordmark">
          <span className="tower-beacon" aria-hidden="true" />
          <div>
            <span className="display tower-wordmark-name">
              <TowerControl size={18} /> Halcyon Tower
            </span>
            <span className="tower-wordmark-tag">operations console · JFK EWR ORD</span>
          </div>
        </div>
        <div className="tower-header-right">
          <PortholeClock />
          <Tooltip label="Back to the traveler page" position="bottom">
            <Link to="/" className="tower-ghost-btn tower-header-btn">
              <Radar size={14} /> Public view
            </Link>
          </Tooltip>
          <button type="button" className="tower-ghost-btn tower-header-btn" onClick={onSignOut}>
            <LogOut size={14} /> Sign out
          </button>
        </div>
      </header>

      <main className="tower-main">
        <div className="tower-tiles">
          {(Object.keys(AIRPORTS) as AirportCode[]).map((code) => (
            <DelayTile key={code} airport={code} />
          ))}
        </div>

        <div className="tower-grid">
          <PollerPanel now={elapsed} />
          <div className="tower-side">
            <EngineTile />
            <ModelCard />
          </div>
        </div>

        <EventFeed events={events} />
      </main>

      <footer className="tower-footer">
        Preview data until the serving layer lands - the layout is wired for the real feeds.
      </footer>
    </div>
  );
}

/* ------------------------------------------------------------------ */

export function AdminPage() {
  const [authed, setAuthed] = useState(() => sessionStorage.getItem(AUTH_KEY) === '1');

  if (!authed) return <TowerLogin onSuccess={() => setAuthed(true)} />;
  return (
    <TowerDashboard
      onSignOut={() => {
        sessionStorage.removeItem(AUTH_KEY);
        setAuthed(false);
      }}
    />
  );
}
