import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// Links in review emails open a case (#case=<id>) or the memberships. Kept across the sign-in redirect.
const link = window.location.hash.match(/^#(case=[A-Za-z0-9]{1,32}|memberships)$/);
if (link) {
  try { sessionStorage.setItem("fdp.link", link[1]); } catch { /* storage unavailable: open the app as usual */ }
  history.replaceState(null, "", window.location.pathname + window.location.search);
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
