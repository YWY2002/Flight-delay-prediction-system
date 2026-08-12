import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { AdminPage } from './pages/AdminPage';
import { PublicPage } from './pages/PublicPage';

/**
 * Public traveler experience lives at "/".
 * The operations console lives at "/admin" behind a (mock) login.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<PublicPage />} />
        <Route path="/admin" element={<AdminPage />} />
      </Routes>
    </BrowserRouter>
  );
}
