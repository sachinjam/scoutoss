import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { login } from "./lib/auth";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);

login()
  .then((session) =>
    root.render(
      <StrictMode>
        <App session={session} />
      </StrictMode>,
    ),
  )
  .catch((err: unknown) => {
    root.render(<p className="error">Sign-in failed: {String(err)}</p>);
  });
