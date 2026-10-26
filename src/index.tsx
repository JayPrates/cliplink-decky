// ClipLink — Decky Loader panel. Networking and clipboard access happen in main.py; this polls its state.

import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaExchangeAlt } from "react-icons/fa";

type Peer = { id: string; name: string; kind: string; ip: string };
type Item = { id: number; dir: "in" | "out"; peer: string; time: number; preview: string; length: number };
type Settings = { auto_copy: boolean; auto_send: boolean; notify: boolean; manual: string; pair_code: string; target: string };
type State = {
  me: { name: string; ips: string[]; port: number };
  peers: Peer[];
  history: Item[];
  settings: Settings;
  clipboard: string;
  listening: boolean;
  status: string;
  error: boolean;
};
type Result = { ok: boolean; message: string };
type Received = { id: number; from: string; copied: boolean; preview: string; notify: boolean; autoCopy: boolean; text?: string };

const getState = callable<[], State>("get_state");
const sendClipboard = callable<[], Result>("send_clipboard");
const sendText = callable<[text: string], Result>("send_text");
const copyItem = callable<[id: number], { ok: boolean; text: string }>("copy_item");
const clearHistory = callable<[], void>("clear_history");
const setSetting = callable<[key: string, value: string | boolean], void>("set_setting");
const refreshNow = callable<[], void>("refresh");

const muted = "#8b929a";
const MAX_SHOWN = 8;

// Fallback when the backend can't reach the X clipboard: let Steam's browser copy it.
function browserCopy(text: string, doc: Document = document): boolean {
  const el = doc.createElement("textarea");
  el.value = text;
  el.style.position = "fixed";
  el.style.opacity = "0";
  doc.body.appendChild(el);
  el.focus();
  el.select();
  let ok = false;
  try { ok = doc.execCommand("copy"); } catch { ok = false; }
  el.remove();
  return ok;
}

function formatWhen(epochSeconds: number) {
  const t = new Date(epochSeconds * 1000);
  if (t.toDateString() === new Date().toDateString())
    return t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toLocaleDateString([], { day: "numeric", month: "short" });
}

function Note({ text, error }: { text: string; error?: boolean }) {
  if (!text) return null;
  return (
    <PanelSectionRow>
      <div style={{ fontSize: "12px", lineHeight: "16px", color: error ? "#ff7068" : muted, padding: "2px 0 6px", wordBreak: "break-word" }}>{text}</div>
    </PanelSectionRow>
  );
}

function PeerList({ peers }: { peers: Peer[] }) {
  return (
    <PanelSectionRow>
      <div style={{ padding: "4px 0 8px" }}>
        {peers.map((p) => (
          <div key={p.id} style={{ display: "flex", alignItems: "center", gap: "8px", lineHeight: "22px" }}>
            <div style={{ width: "8px", height: "8px", borderRadius: "4px", background: "#46d478", flexShrink: 0 }} />
            <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</div>
            <div style={{ fontSize: "12px", color: muted, marginLeft: "auto" }}>{p.ip}</div>
          </div>
        ))}
      </div>
    </PanelSectionRow>
  );
}

// A text setting that's saved when the field loses focus, not on every keystroke.
function SettingText({ label, description, value, onSave, password }: {
  label: string; description: string; value: string; onSave: (v: string) => void; password?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <PanelSectionRow>
      <TextField
        label={label}
        description={description}
        value={draft}
        bIsPassword={password}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { if (draft.trim() !== value) onSave(draft.trim()); }}
      />
    </PanelSectionRow>
  );
}

function Content() {
  const [s, setS] = useState<State | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  const refresh = async () => {
    try {
      setS(await getState());
      setBackendError(null);
    } catch (e) {
      console.error("ClipLink", e);
      setBackendError(String(e));
    }
  };

  useEffect(() => {
    refreshNow().catch((e) => console.error("ClipLink", e));
    refresh();
    const timer = setInterval(refresh, 1000);
    const listener = addEventListener<[Received]>("cliplink_received", () => { refresh(); });
    return () => { clearInterval(timer); removeEventListener("cliplink_received", listener); };
  }, []);

  const send = async (f: () => Promise<Result>) => {
    setBusy(true);
    try {
      const r = await f();
      setMessage({ text: r.message, error: !r.ok });
      return r.ok;
    } catch (e) {
      setMessage({ text: String(e), error: true });
      return false;
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const save = async (key: keyof Settings, value: string | boolean) => { await setSetting(key, value); await refresh(); };

  const copy = async (item: Item, doc?: Document) => {
    const r = await copyItem(item.id);
    let ok = r.ok;
    if (!ok && r.text) ok = browserCopy(r.text, doc);
    setMessage({ text: ok ? "Copied to the clipboard." : "Couldn't reach the clipboard.", error: !ok });
    refresh();
  };

  if (!s) {
    return (
      <PanelSection>
        <Note
          text={backendError ? "The plugin's backend isn't responding: " + backendError + ". Check ~/homebrew/logs/ClipLink/." : "Loading…"}
          error={!!backendError}
        />
      </PanelSection>
    );
  }

  const shown = s.history.slice(0, MAX_SHOWN);
  const status = message ?? (s.status ? { text: s.status, error: s.error } : null);
  const targetOptions = [{ data: "", label: "All devices" }, ...s.peers.map((p) => ({ data: p.id, label: p.name }))];

  return (
    <>
      <PanelSection title="Devices">
        {s.peers.length > 0
          ? <PeerList peers={s.peers} />
          : <Note text={s.settings.manual
              ? `Looking for ClipLink at ${s.settings.manual}…`
              : "Looking for your PC… Open ClipLink on it, on the same network as this Deck."} />}
        {s.peers.length > 1 && (
          <PanelSectionRow>
            <DropdownItem
              label="Send to"
              rgOptions={targetOptions}
              selectedOption={s.peers.some((p) => p.id === s.settings.target) ? s.settings.target : ""}
              onChange={(o) => save("target", o.data)}
            />
          </PanelSectionRow>
        )}
        {!s.listening && <Note text={`Can't receive: port ${s.me.port} is busy.`} error />}
      </PanelSection>

      <PanelSection title="Send">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => send(sendClipboard)}>Send clipboard</ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField label="Or type something" value={text} onChange={(e) => setText(e.target.value)} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || !text.trim()}
            onClick={async () => { if (await send(() => sendText(text))) setText(""); }}
          >
            Send text
          </ButtonItem>
        </PanelSectionRow>
        {status && <Note text={status.text} error={status.error} />}
      </PanelSection>

      <PanelSection title="Recent">
        {shown.length === 0 && <Note text="Text you send or receive shows up here. Select an entry to copy it again." />}
        {shown.map((item) => (
          <PanelSectionRow key={item.id}>
            <ButtonItem
              layout="below"
              label={item.preview}
              description={`${item.dir === "in" ? "From" : "To"} ${item.peer} · ${formatWhen(item.time)}${item.length > 120 ? ` · ${item.length.toLocaleString()} characters` : ""}`}
              onClick={(e: any) => copy(item, e?.currentTarget?.ownerDocument)}
            >
              Copy
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {shown.length > 0 && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={async () => { await clearHistory(); setMessage(null); refresh(); }}>Clear</ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <ToggleField
            label="Copy received text"
            description="Text sent from the PC goes straight to this Deck's clipboard"
            checked={s.settings.auto_copy}
            onChange={(on) => save("auto_copy", on)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Send what I copy"
            description="Anything you copy on the Deck is sent to the PC automatically"
            checked={s.settings.auto_send}
            onChange={(on) => save("auto_send", on)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Notifications"
            description="Show a notification when text arrives"
            checked={s.settings.notify}
            onChange={(on) => save("notify", on)}
          />
        </PanelSectionRow>
        <SettingText
          label="PC address"
          description="Only needed if your PC isn't found automatically, e.g. 192.168.1.20"
          value={s.settings.manual}
          onSave={(v) => save("manual", v)}
        />
        <SettingText
          label="Pairing code"
          description="Optional. When set, only devices using the same code can send text here"
          value={s.settings.pair_code}
          onSave={(v) => save("pair_code", v)}
          password
        />
        <Note text={`This device: ${s.me.name}${s.me.ips.length ? " · " + s.me.ips.join(", ") : ""}. ${s.clipboard}`} />
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Registered for the plugin's lifetime so text arriving while the menu is closed still shows a notification.
  const onReceived = addEventListener<[Received]>("cliplink_received", (r) => {
    let copied = r.copied;
    if (!copied && r.text) copied = browserCopy(r.text);
    if (!r.notify) return;
    toaster.toast({
      title: copied ? `Copied text from ${r.from}` : `Text from ${r.from}`,
      body: copied || !r.autoCopy ? r.preview : "Couldn't copy it automatically — open ClipLink to copy it.",
    });
  });
  return {
    name: "ClipLink",
    titleView: <div className={staticClasses.Title}>ClipLink</div>,
    content: <Content />,
    icon: <FaExchangeAlt />,
    onDismount() { removeEventListener("cliplink_received", onReceived); },
  };
});
