import { useMemo, useState } from 'react';
import { SegmentedControl, TextInput } from '@mantine/core';
import { AnimatePresence, motion } from 'framer-motion';
import { Feather, Search } from 'lucide-react';
import { FlightBoard, FlightRow } from '../components/FlightBoard';
import { FlightModal } from '../components/FlightModal';
import { PortholeClock } from '../components/PortholeClock';
import { SkyBackground } from '../components/SkyBackground';
import { SystemPulse } from '../components/SystemPulse';
import {
  AIRPORTS,
  searchByFlightNumber,
  type AirportCode,
  type Flight,
} from '../data/flights';
import './PublicPage.css';

type Mode = 'airport' | 'flight';

const fade = {
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -10 },
  transition: { duration: 0.28 },
};

export function PublicPage() {
  const [mode, setMode] = useState<Mode>('airport');
  const [airport, setAirport] = useState<AirportCode | null>(null);
  const [flightQuery, setFlightQuery] = useState('');
  const [selected, setSelected] = useState<Flight | null>(null);

  const flightMatches = useMemo(
    () => (flightQuery.trim().length >= 2 ? searchByFlightNumber(flightQuery).slice(0, 12) : []),
    [flightQuery],
  );

  return (
    <div className="page">
      <SkyBackground />
      <SystemPulse />

      <header className="page-header">
        <div className="wordmark">
          <Feather size={20} aria-hidden="true" />
          <div>
            <span className="display wordmark-name">Halcyon</span>
            <span className="wordmark-tag">know before you go</span>
          </div>
        </div>
        <PortholeClock />
      </header>

      <main className="page-main">
        <section className="hero">
          <motion.h1
            className="display hero-title"
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
          >
            Your flight, without
            <br />
            the wondering.
          </motion.h1>
          <motion.p
            className="hero-sub"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.25, duration: 0.6 }}
          >
            A calm forecast of how likely your flight is to run late, drawn from live weather and
            airport conditions. No refresh-refreshing required.
          </motion.p>

          <motion.div
            className="hero-mode"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.4, duration: 0.5 }}
          >
            <SegmentedControl
              value={mode}
              onChange={(v) => setMode(v as Mode)}
              data={[
                { value: 'airport', label: 'Browse an airport' },
                { value: 'flight', label: 'Find my flight' },
              ]}
              radius="xl"
              size="md"
            />
          </motion.div>
        </section>

        <AnimatePresence mode="wait">
          {mode === 'airport' ? (
            <motion.div key="airport" {...fade}>
              <div className="airport-cards" role="list">
                {(Object.keys(AIRPORTS) as AirportCode[]).map((code) => {
                  const a = AIRPORTS[code];
                  const active = airport === code;
                  return (
                    <button
                      key={code}
                      type="button"
                      role="listitem"
                      className={`airport-card glass glass-hover ${active ? 'airport-card-active' : ''}`}
                      onClick={() => setAirport(active ? null : code)}
                      aria-pressed={active}
                    >
                      <span className="mono airport-card-code">{a.iata}</span>
                      <span className="display airport-card-city">{a.city}</span>
                      <span className="airport-card-blurb">{a.blurb}</span>
                      <span className="mono airport-card-icao">{a.code}</span>
                    </button>
                  );
                })}
              </div>

              <AnimatePresence>
                {airport && (
                  <motion.div
                    key={airport}
                    initial={{ opacity: 0, y: 24 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 12 }}
                    transition={{ duration: 0.35 }}
                    className="board-wrap"
                  >
                    <FlightBoard airport={airport} onSelect={setSelected} />
                  </motion.div>
                )}
              </AnimatePresence>

              {!airport && (
                <p className="airport-nudge">Pick an airport to see today's board.</p>
              )}
            </motion.div>
          ) : (
            <motion.div key="flight" {...fade} className="flight-search">
              <TextInput
                value={flightQuery}
                onChange={(e) => setFlightQuery(e.currentTarget.value)}
                placeholder="Try DL 447 or UA 1289"
                leftSection={<Search size={16} />}
                size="lg"
                radius="xl"
                autoFocus
                className="flight-search-input"
                aria-label="Search by flight number"
              />
              <div className="flight-search-results" role="list">
                <AnimatePresence initial={false}>
                  {flightMatches.map((f) => (
                    <FlightRow key={f.id} flight={f} onSelect={setSelected} />
                  ))}
                </AnimatePresence>
                {flightQuery.trim().length >= 2 && flightMatches.length === 0 && (
                  <p className="flight-search-empty">
                    Nothing under that number today across JFK, EWR, and ORD. Double-check the
                    airline code, or browse the airport instead.
                  </p>
                )}
                {flightQuery.trim().length < 2 && (
                  <p className="flight-search-empty">
                    Type a flight number and we'll look across all three airports.
                  </p>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </main>

      <footer className="page-footer">
        Halcyon watches JFK, Newark, and O'Hare. Forecasts are probabilities, not promises - but
        they're honest ones.
      </footer>

      <FlightModal flight={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
