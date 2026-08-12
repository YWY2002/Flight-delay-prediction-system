import { Modal, ScrollArea } from '@mantine/core';
import { motion } from 'framer-motion';
import {
  PlaneTakeoff,
  PlaneLanding,
  Clock3,
  DoorOpen,
  Armchair,
  CloudDrizzle,
} from 'lucide-react';
import { AIRPORTS, RISK_COPY, riskBand, type Flight } from '../data/flights';
import './FlightModal.css';

function fmtTime(d: Date): string {
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
}

const STATUS_LABEL: Record<Flight['status'], string> = {
  scheduled: 'On schedule',
  boarding: 'Boarding',
  departed: 'Departed',
  'en-route': 'En route',
  landed: 'Landed',
  delayed: 'Delayed',
  cancelled: 'Cancelled',
};

function Gauge({ probability }: { probability: number }) {
  const band = riskBand(probability);
  const pct = Math.round(probability * 100);
  const color =
    band === 'low' ? 'var(--calm-mint)' : band === 'medium' ? 'var(--calm-amber)' : 'var(--calm-coral)';

  // Semi-circular arc from -120deg to +120deg.
  const sweep = 240;
  const r = 74;
  const circumference = (sweep / 360) * 2 * Math.PI * r;
  const filled = circumference * probability;

  return (
    <div className="gauge" role="img" aria-label={`${pct} percent chance of running late`}>
      <svg viewBox="0 0 200 156">
        {/* An SVG circle's dash starts at 3 o'clock and runs clockwise, so the
            240deg sweep is rotated to start at 150deg. That leaves the 120deg
            opening centred on 6 o'clock and keeps the arc clear of the bottom
            edge - rotating the other way swings it through 6 o'clock, where
            the viewBox clips it flat. */}
        <g transform="rotate(150 100 100)">
          <circle
            cx="100"
            cy="100"
            r={r}
            fill="none"
            stroke="rgba(244,240,232,0.12)"
            strokeWidth="12"
            strokeLinecap="round"
            strokeDasharray={`${circumference} ${2 * Math.PI * r}`}
          />
          <motion.circle
            cx="100"
            cy="100"
            r={r}
            fill="none"
            stroke={color}
            strokeWidth="12"
            strokeLinecap="round"
            strokeDasharray={`${filled} ${2 * Math.PI * r}`}
            initial={{ strokeDasharray: `0 ${2 * Math.PI * r}` }}
            animate={{ strokeDasharray: `${filled} ${2 * Math.PI * r}` }}
            transition={{ duration: 1.1, ease: [0.22, 1, 0.36, 1] }}
          />
        </g>
        <text x="100" y="112" textAnchor="middle" className="gauge-number">
          {pct}%
        </text>
      </svg>
    </div>
  );
}

export function FlightModal({ flight, onClose }: { flight: Flight | null; onClose: () => void }) {
  const isDeparture = flight?.direction === 'departure';

  return (
    <Modal
      opened={flight !== null}
      onClose={onClose}
      centered
      size="lg"
      radius={24}
      padding={0}
      withCloseButton={false}
      scrollAreaComponent={ScrollArea.Autosize}
      overlayProps={{ blur: 6, color: '#0d1028', backgroundOpacity: 0.6 }}
      classNames={{ content: 'flight-modal', body: 'flight-modal-body' }}
      transitionProps={{ transition: 'pop', duration: 220 }}
    >
      {flight && (
        <>
          <header className="flight-modal-header">
            <div>
              <p className="eyebrow">
                {isDeparture ? 'Departure' : 'Arrival'} · {AIRPORTS[flight.airport].iata}
              </p>
              <h2 className="display flight-modal-number mono">{flight.flightNumber}</h2>
              <p className="flight-modal-airline">{flight.airline.name}</p>
            </div>
            <button type="button" className="flight-modal-close" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </header>

          <div className="flight-modal-route">
            <div className="flight-modal-endpoint">
              <span className="mono flight-modal-code">
                {isDeparture ? AIRPORTS[flight.airport].iata : flight.counterpartCode}
              </span>
              <span className="flight-modal-city">
                {isDeparture ? AIRPORTS[flight.airport].city : flight.counterpartCity}
              </span>
            </div>
            <div className="flight-modal-path" aria-hidden="true">
              {isDeparture ? <PlaneTakeoff size={20} /> : <PlaneLanding size={20} />}
              <span className="flight-modal-dashes" />
            </div>
            <div className="flight-modal-endpoint flight-modal-endpoint-right">
              <span className="mono flight-modal-code">
                {isDeparture ? flight.counterpartCode : AIRPORTS[flight.airport].iata}
              </span>
              <span className="flight-modal-city">
                {isDeparture ? flight.counterpartCity : AIRPORTS[flight.airport].city}
              </span>
            </div>
          </div>

          <div className="flight-modal-grid">
            <div className="flight-modal-facts">
              <div className="flight-fact">
                <Clock3 size={16} />
                <div>
                  <span className="flight-fact-label">Scheduled</span>
                  <span className="mono">{fmtTime(flight.scheduled)}</span>
                </div>
              </div>
              <div className="flight-fact">
                <Clock3 size={16} />
                <div>
                  <span className="flight-fact-label">Estimated</span>
                  <span className="mono">
                    {fmtTime(flight.estimated)}
                    {flight.expectedDelayMin > 0 && (
                      <em className="flight-fact-late"> +{flight.expectedDelayMin}m</em>
                    )}
                  </span>
                </div>
              </div>
              <div className="flight-fact">
                <DoorOpen size={16} />
                <div>
                  <span className="flight-fact-label">Terminal · Gate</span>
                  <span className="mono">
                    T{flight.terminal} · {flight.gate}
                  </span>
                </div>
              </div>
              <div className="flight-fact">
                <Armchair size={16} />
                <div>
                  <span className="flight-fact-label">Aircraft</span>
                  <span>{flight.aircraft}</span>
                </div>
              </div>
              <div className="flight-fact">
                <span className={`status-dot status-${flight.status}`} />
                <div>
                  <span className="flight-fact-label">Status</span>
                  <span>{STATUS_LABEL[flight.status]}</span>
                </div>
              </div>
            </div>

            <div className="flight-modal-forecast">
              <Gauge probability={flight.delayProbability} />
              <p className="flight-modal-reassure">{RISK_COPY[riskBand(flight.delayProbability)]}</p>
              {flight.drivers.length > 0 && (
                <ul className="flight-modal-drivers">
                  {flight.drivers.map((d) => (
                    <li key={d}>
                      <CloudDrizzle size={14} /> {d}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <footer className="flight-modal-footer">
            Forecast preview - live model predictions arrive when the engine comes online.
          </footer>
        </>
      )}
    </Modal>
  );
}
