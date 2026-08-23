import { useEffect, useState } from "react";
import FileUpload from "./components/FileUpload";
import ChatInterface from "./components/ChatInterface";
import Login from "./components/Login";
import { checkHealth, clearToken, getToken } from "./api";
import "./App.css";

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [userEmail, setUserEmail] = useState("");
  const [authChecked, setAuthChecked] = useState(false);
  const [tables, setTables] = useState([]);
  const [backendStatus, setBackendStatus] = useState("checking"); // checking | ok | down

  // On load, if a token already exists (from a previous session), treat
  // the user as logged in without making them re-enter credentials.
  // We don't have a dedicated "whoami" endpoint, so this is optimistic —
  // if the token's actually expired, the first API call's 401 handler
  // (in api.js) will clear it and reload, kicking back to the login page.
  useEffect(() => {
    setIsAuthenticated(Boolean(getToken()));
    setAuthChecked(true);
  }, []);

  useEffect(() => {
    if (!isAuthenticated) return;
    checkHealth()
      .then((data) => {
        setTables(data.tables || []);
        setBackendStatus("ok");
      })
      .catch(() => setBackendStatus("down"));
  }, [isAuthenticated]);

  function handleUploaded(result) {
    setTables((prev) => [...prev, result.table_name]);
  }

  function handleAuthenticated(email) {
    setUserEmail(email);
    setIsAuthenticated(true);
  }

  function handleLogout() {
    clearToken();
    window.location.reload();
  }

  if (!authChecked) return null; // avoid a login-page flash while checking localStorage

  if (!isAuthenticated) {
    return <Login onAuthenticated={handleAuthenticated} />;
  }

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__title-block">
          <h1 className="app__title">AI Data Analyst</h1>
          <p className="app__subtitle">Ask your data a question. Get back SQL, an answer, and a chart.</p>
        </div>
        <div className="app__header-actions">
          <FileUpload onUploaded={handleUploaded} />
          <button className="app__logout" onClick={handleLogout}>
            {userEmail ? `Log out (${userEmail})` : "Log out"}
          </button>
        </div>
      </header>

      <div className="app__status-bar">
        {backendStatus === "checking" && <span>Connecting to backend…</span>}
        {backendStatus === "down" && (
          <span className="app__status-bar--error">
            Can't reach the backend. Is it running? (docker compose up)
          </span>
        )}
        {backendStatus === "ok" && (
          <span>
            {tables.length === 0
              ? "No tables loaded yet"
              : `${tables.length} table${tables.length === 1 ? "" : "s"} available: ${tables.join(", ")}`}
          </span>
        )}
      </div>

      <main className="app__main">
        <ChatInterface hasData={tables.length > 0} tables={tables} />
      </main>
    </div>
  );
}