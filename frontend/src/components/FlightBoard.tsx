import { useMemo, useState } from 'react';
import { MultiSelect, ScrollArea, SegmentedControl, Select, TextInput } from '@mantine/core';
import { AnimatePresence, motion } from 'framer-motion';
import { PlaneLanding, PlaneTakeoff, Search, Wind } from 'lucide-react';
import {
  AIRPORTS,
  flightsFor,
  riskBand,
  type AirportCode,
  type Direction,
  type Flight,
} from '../data/flights';
import './FlightBoard.css';

type TimeWindow = 'all' | 'soon' | 'morning' | 'afternoon' | 'evening';

const STATUS_FILTERS = [
  { value: 'all', label: 'Any status' },
  { value: 'active', label: 'Still to fly' },
  { value: 'delayed', label: 'Delayed only' },
  { value: 'done', label: 'Already flown' },
];

const WINDOW_LABELS: Array<{ value: TimeWindow; label: string }> = [
  { value: 'all', label: 'All day' },
  { value: 'soon', label: 'Next 3h' },
  { value: 'morning', label: 'Morning' },
  { value: 'afternoon', label: 'Afternoon' },
  { value: 'evening', label: 'Evening' },
];

function inWindow(f: Flight, w: TimeWindow): boolean {
  const h = f.scheduled.getHours();
  switch (w) {
    case 'all':
      return true;
    case 'soon': {
      const dt = f.scheduled.getTime() - Date.now();
      return dt >= -15 * 60000 && dt <= 3 * 3600 * 1000;
    }
    case 'morning':
      return h >= 5 && h < 12;
    case 'afternoon':
      return h >= 12 && h < 18;
    case 'evening':
      return h >= 18;
  }
}

function matchesStatus(f: Flight, s: string): boolean {
  switch (s) {
    case 'active':
      return ['scheduled', 'boarding', 'delayed', 'en-route'].includes(f.status);
    case 'delayed':
      return f.status === 'delayed';
    case 'done':
      return ['departed', 'landed', 'cancelled'].includes(f.status);
    default:
      return true;
  }
}

function fmtTime(d: Date): string {
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
}

const STATUS_TEXT: Record<Flight['status'], string> = {
  scheduled: 'On schedule',
  boarding: 'Boarding',
  departed: 'Departed',
  'en-route': 'En route',
  landed: 'Landed',
  delayed: 'Delayed',
  cancelled: 'Cancelled',
};

export function FlightRow({ flight, onSelect }: { flight: Flight; onSelect: (f: Flight) => void }) {
  const band = riskBand(flight.delayProbability);
  return (
    <motion.button
      layout
      type="button"
      className="flight-row glass-hover"
      onClick={() => onSelect(flight)}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
      transition={{ duration: 0.22 }}
    >
      <span className="mono flight-row-time">{fmtTime(flight.scheduled)}</span>
      <span
        className="flight-row-airline mono"
        style={{ background: `hsla(${flight.airline.hue}, 55%, 62%, 0.18)` }}
        title={flight.airline.name}
      >
        {flight.airline.code}
      </span>
      <span className="mono flight-row-number">{flight.flightNumber}</span>
      <span className="flight-row-city">
        {flight.counterpartCity}
        <em className="mono">{flight.counterpartCode}</em>
      </span>
      <span className="mono flight-row-gate">
        T{flight.terminal}·{flight.gate}
      </span>
      <span className="flight-row-status">
        <span className={`status-dot status-${flight.status}`} />
        {STATUS_TEXT[flight.status]}
      </span>
      <span className={`flight-row-risk risk-${band}`}>
        {Math.round(flight.delayProbability * 100)}%
        <em>late risk</em>
      </span>
    </motion.button>
  );
}

export function FlightBoard({
  airport,
  onSelect,
}: {
  airport: AirportCode;
  onSelect: (f: Flight) => void;
}) {
  const [direction, setDirection] = useState<Direction>('departure');
  const [airlineFilter, setAirlineFilter] = useState<string[]>([]);
  const [statusFilter, setStatusFilter] = useState('active');
  const [window, setWindow] = useState<TimeWindow>('all');
  const [query, setQuery] = useState('');

  const flights = useMemo(() => flightsFor(airport, direction), [airport, direction]);

  const airlineOptions = useMemo(() => {
    const seen = new Map<string, string>();
    flights.forEach((f) => seen.set(f.airline.code, f.airline.name));
    return [...seen.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [flights]);

  const visible = useMemo(() => {
    const q = query.trim().toUpperCase();
    return flights.filter(
      (f) =>
        matchesStatus(f, statusFilter) &&
        inWindow(f, window) &&
        (airlineFilter.length === 0 || airlineFilter.includes(f.airline.code)) &&
        (q === '' ||
          f.flightNumber.replace(/\s/g, '').includes(q.replace(/\s/g, '')) ||
          f.counterpartCity.toUpperCase().includes(q) ||
          f.counterpartCode.includes(q)),
    );
  }, [flights, statusFilter, window, airlineFilter, query]);

  return (
    <section className="board glass" aria-label={`Flights at ${AIRPORTS[airport].name}`}>
      <header className="board-head">
        <div>
          <p className="eyebrow">{AIRPORTS[airport].name}</p>
          <h2 className="display board-title">
            {AIRPORTS[airport].iata}
            <span className="board-title-sub">{AIRPORTS[airport].city}</span>
          </h2>
        </div>
        <SegmentedControl
          value={direction}
          onChange={(v) => setDirection(v as Direction)}
          data={[
            { value: 'departure', label: 'Departures' },
            { value: 'arrival', label: 'Arrivals' },
          ]}
          radius="xl"
          classNames={{ root: 'board-direction' }}
        />
      </header>

      <div className="board-filters">
        <TextInput
          value={query}
          onChange={(e) => setQuery(e.currentTarget.value)}
          placeholder="Flight, city, or code"
          leftSection={<Search size={15} />}
          radius="xl"
          className="board-filter-search"
        />
        <MultiSelect
          data={airlineOptions}
          value={airlineFilter}
          onChange={setAirlineFilter}
          placeholder={airlineFilter.length === 0 ? 'All airlines' : undefined}
          radius="xl"
          searchable
          clearable
          className="board-filter-airline"
        />
        <Select
          data={STATUS_FILTERS}
          value={statusFilter}
          onChange={(v) => setStatusFilter(v ?? 'all')}
          radius="xl"
          allowDeselect={false}
          className="board-filter-status"
        />
        <SegmentedControl
          value={window}
          onChange={(v) => setWindow(v as TimeWindow)}
          data={WINDOW_LABELS}
          radius="xl"
          size="xs"
          className="board-filter-window"
        />
      </div>

      <div className="board-legend" aria-hidden="true">
        <span>Time</span>
        <span />
        <span>Flight</span>
        <span>{direction === 'departure' ? 'To' : 'From'}</span>
        <span>Gate</span>
        <span>Status</span>
        <span>Forecast</span>
      </div>

      <ScrollArea.Autosize mah="56vh" type="hover" offsetScrollbars="present">
        <div className="board-list" role="list">
          <AnimatePresence initial={false}>
            {visible.map((f) => (
              <FlightRow key={f.id} flight={f} onSelect={onSelect} />
            ))}
          </AnimatePresence>
          {visible.length === 0 && (
            <div className="board-empty">
              <span className="board-empty-icon" aria-hidden="true">
                {direction === 'departure' ? <PlaneTakeoff size={26} /> : <PlaneLanding size={26} />}
              </span>
              <p>No flights match these filters.</p>
              <p className="board-empty-hint">
                <Wind size={13} /> Try widening the time window or clearing the airline filter.
              </p>
            </div>
          )}
        </div>
      </ScrollArea.Autosize>
    </section>
  );
}
