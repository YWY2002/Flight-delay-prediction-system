# Halcyon - public frontend

The traveler-facing page for the flight delay prediction system. React 19 +
TypeScript + Vite, with Mantine for interactive components (modal, drawer,
popover, selects), framer-motion for transitions, and self-hosted fonts via
Fontsource.

## Run it

```bash
npm install
npm run dev
```

Then open http://localhost:5173.

## What's here

- `/` - the public page.
- `/admin` - "Halcyon Tower", the operations console, behind a mock login
  (demo access: `admin` / `tower`, kept in sessionStorage). Shows per-airport
  delay-index tiles with 24 h sparklines, the four ingestion pollers, serving
  API health, the model card, and a live approach-anomaly feed - all preview
  data shaped like the real Phase 1 feeds.
- **Browse an airport** - JFK / EWR / ORD cards open a departures/arrivals
  board with filters (search, airline, status, time window).
- **Find my flight** - flight-number search across all three airports.
- Clicking any flight opens a detail popup with a delay-probability gauge.
- **Engine tab** (right edge) - polls `VITE_API_BASE`/health (default
  `http://localhost:8000`) every 30 s and shows whether the backend is alive.
  Offline is a designed state, not an error.
- **Porthole clock** (top right) - analog clock, defaults to Singapore time;
  click it to switch region.

## Data

`src/data/flights.ts` generates a deterministic mock schedule per day so the
board is stable across reloads. Swap this module for real API calls when the
serving layer (Phase 5) exists.
