import {render} from "preact";
import {App} from "./App";
import "./styles.css";

try {
  const preferences = JSON.parse(localStorage.getItem("mergerail-ui-prefs") || "null") as {
    theme?: unknown;
    density?: unknown;
  } | null;
  if (preferences?.theme === "light" || preferences?.theme === "dark") {
    document.documentElement.dataset.theme = preferences.theme;
  }
  if (preferences?.density === "compact" || preferences?.density === "comfortable") {
    document.documentElement.dataset.density = preferences.density;
  }
} catch {}

render(<App />, document.getElementById("app")!);
