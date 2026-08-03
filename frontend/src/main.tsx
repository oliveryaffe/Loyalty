import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
// Self-hosted Inter (SIL OFL 1.1) via @fontsource -- Vite bundles/hashes
// these .woff2 files at build time, replacing the Google Fonts CDN
// <link> tags that used to load the same weights from
// fonts.googleapis.com/fonts.gstatic.com (see index.html, GDPR pass).
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/inter/800.css";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
