"use client";

import { useEffect, useRef, useState } from "react";
import { api, type ModelInfo } from "@/lib/api";
import { useSpeech } from "@/lib/useSpeech";

export interface Attachment {
  mime_type: string;
  data: string; // base64, no data: prefix
  name: string;
  preview: string; // full data URL, for the chip thumbnail
}

export interface SubmitOptions {
  model: string;
  attachments: Attachment[];
}

const MODEL_KEY = "inflight-model";

/**
 * The one place a prompt is written. Never disabled by work in progress — the
 * whole product rests on being able to send again while answers stream.
 *
 * Owns the model choice, image attachments, and the two voice inputs, and hands
 * all of it to `onSubmit`. `disabled` covers only the brief moment a hero prompt
 * is creating its conversation, never a streaming answer.
 */
export function Composer({
  onSubmit,
  placeholder = "Reply, or ask something new",
  disabled = false,
  autoFocus = false,
  footer,
  banner,
}: {
  onSubmit: (prompt: string, options: SubmitOptions) => void | Promise<void>;
  placeholder?: string;
  disabled?: boolean;
  autoFocus?: boolean;
  footer?: React.ReactNode;
  banner?: React.ReactNode;
}) {
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const field = useRef<HTMLTextAreaElement>(null);

  const insert = (text: string) =>
    setDraft((d) => (d ? `${d} ${text}` : text));
  const speech = useSpeech(insert);

  // Grow with the text up to a ceiling, then scroll.
  useEffect(() => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  }, [draft]);

  const [model, setModel] = useState<string>("");
  useEffect(() => {
    api
      .models()
      .then((data) => {
        const saved = localStorage.getItem(MODEL_KEY);
        const valid = saved && data.models.some((m) => m.id === saved);
        setModel(valid ? saved! : data.default);
      })
      .catch(() => {});
  }, []);

  function chooseModel(id: string) {
    setModel(id);
    localStorage.setItem(MODEL_KEY, id);
  }

  function submit() {
    const prompt = draft.trim();
    if ((!prompt && attachments.length === 0) || disabled) return;
    setDraft("");
    const sent = attachments;
    setAttachments([]);
    void onSubmit(prompt || "(see attached)", { model, attachments: sent });
  }

  return (
    <div className="rounded-2xl border border-edge bg-surface shadow-composer">
      {banner}

      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 px-3 pt-3">
          {attachments.map((a, i) => (
            <div key={i} className="group relative">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={a.preview}
                alt={a.name}
                className="h-14 w-14 rounded-lg border border-edge object-cover"
              />
              <button
                onClick={() => setAttachments((list) => list.filter((_, j) => j !== i))}
                className="absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full bg-ink text-[10px] text-bg"
                aria-label="Remove attachment"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      <textarea
        ref={field}
        value={draft}
        autoFocus={autoFocus}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        rows={1}
        placeholder={speech.listening ? "Listening…" : placeholder}
        className="max-h-[260px] min-h-[56px] w-full resize-none bg-transparent px-4 pt-4 text-[15px] leading-relaxed outline-none placeholder:text-ink-faint"
      />

      <div className="flex items-center gap-1 px-2.5 pb-2.5 pt-1">
        <AttachMenu
          onImages={(added) => setAttachments((list) => [...list, ...added].slice(0, 6))}
          onGithub={(text) => insert(text)}
        />

        <div className="ml-auto flex items-center gap-1">
          <ModelPicker model={model} onChange={chooseModel} />

          {speech.supported && (
            <>
              <button
                type="button"
                title="Hold to talk"
                aria-label="Hold to talk"
                onMouseDown={() => speech.start(false)}
                onMouseUp={() => speech.stop()}
                onMouseLeave={() => speech.listening && speech.stop()}
                onTouchStart={(e) => {
                  e.preventDefault();
                  speech.start(false);
                }}
                onTouchEnd={() => speech.stop()}
                className={`rounded-lg p-2 transition-colors ${
                  speech.listening
                    ? "bg-flight/10 text-flight"
                    : "text-ink-faint hover:bg-surface-2 hover:text-ink"
                }`}
              >
                <MicIcon />
              </button>
              <button
                type="button"
                title="Dictate — click to start, click to stop"
                aria-label="Toggle dictation"
                onClick={() => (speech.listening ? speech.stop() : speech.start(true))}
                className={`rounded-lg p-2 transition-colors ${
                  speech.listening
                    ? "bg-ember/10 text-ember"
                    : "text-ink-faint hover:bg-surface-2 hover:text-ink"
                }`}
              >
                <WaveIcon />
              </button>
            </>
          )}

          <button
            type="button"
            onClick={submit}
            disabled={(!draft.trim() && attachments.length === 0) || disabled}
            aria-label="Send"
            className="ml-0.5 rounded-full bg-flight p-2 text-white transition-opacity disabled:opacity-25"
          >
            <SendIcon />
          </button>
        </div>
      </div>

      {footer && <div className="border-t border-edge/70 px-4 py-2">{footer}</div>}
    </div>
  );
}

/* ---- attach (+) menu: file/photo, camera, github ------------------------ */

async function fileToAttachment(file: File): Promise<Attachment | null> {
  if (!file.type.startsWith("image/")) return null;
  const preview = await new Promise<string>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.readAsDataURL(file);
  });
  return {
    mime_type: file.type,
    data: preview.split(",")[1] ?? "",
    name: file.name,
    preview,
  };
}

function AttachMenu({
  onImages,
  onGithub,
}: {
  onImages: (added: Attachment[]) => void;
  onGithub: (text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [camera, setCamera] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function onFiles(files: FileList | null) {
    if (!files) return;
    const parsed = await Promise.all(Array.from(files).map(fileToAttachment));
    onImages(parsed.filter((a): a is Attachment => a !== null));
  }

  async function addGithub() {
    setOpen(false);
    const url = window.prompt("Paste a GitHub file URL (blob or raw):");
    if (!url) return;
    try {
      const raw = url
        .replace("github.com", "raw.githubusercontent.com")
        .replace("/blob/", "/");
      const res = await fetch(raw);
      if (!res.ok) throw new Error(`could not fetch (${res.status})`);
      const text = (await res.text()).slice(0, 12000);
      const name = raw.split("/").slice(-1)[0] || "file";
      onGithub(`Here is \`${name}\` for reference:\n\n\`\`\`\n${text}\n\`\`\`\n`);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "could not fetch that URL");
    }
  }

  return (
    <div className="relative">
      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        multiple
        hidden
        onChange={(e) => {
          void onFiles(e.target.files);
          e.target.value = "";
        }}
      />

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Add attachment"
        className="rounded-lg p-2 text-ink-faint transition-colors hover:bg-surface-2 hover:text-ink"
      >
        <PlusIcon />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full left-0 z-20 mb-2 w-52 overflow-hidden rounded-xl border border-edge bg-surface py-1 shadow-composer">
            <MenuRow
              onClick={() => {
                setOpen(false);
                fileRef.current?.click();
              }}
              icon={<ImageIcon />}
            >
              Add files or photos
            </MenuRow>
            <MenuRow
              onClick={() => {
                setOpen(false);
                setCamera(true);
              }}
              icon={<CameraIcon />}
            >
              Take a photo
            </MenuRow>
            <MenuRow onClick={addGithub} icon={<GithubIcon />}>
              Add from GitHub
            </MenuRow>
          </div>
        </>
      )}

      {camera && (
        <CameraCapture
          onClose={() => setCamera(false)}
          onCapture={(a) => {
            onImages([a]);
            setCamera(false);
          }}
        />
      )}
    </div>
  );
}

function CameraCapture({
  onClose,
  onCapture,
}: {
  onClose: () => void;
  onCapture: (a: Attachment) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  useEffect(() => {
    let active = true;
    navigator.mediaDevices
      ?.getUserMedia({ video: { facingMode: "environment" } })
      .then((stream) => {
        if (!active) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) videoRef.current.srcObject = stream;
      })
      .catch(() => setError("Could not access the camera."));
    return () => {
      active = false;
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  function shoot() {
    const video = videoRef.current;
    if (!video) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    const preview = canvas.toDataURL("image/jpeg", 0.85);
    onCapture({
      mime_type: "image/jpeg",
      data: preview.split(",")[1] ?? "",
      name: `photo-${Date.now()}.jpg`,
      preview,
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-md overflow-hidden rounded-2xl border border-edge bg-surface">
        <div className="aspect-video bg-black">
          {error ? (
            <div className="flex h-full items-center justify-center text-sm text-failed">
              {error}
            </div>
          ) : (
            <video ref={videoRef} autoPlay playsInline className="h-full w-full object-cover" />
          )}
        </div>
        <div className="flex justify-between gap-2 p-3">
          <button
            onClick={onClose}
            className="rounded-lg px-3 py-2 text-sm text-ink-soft hover:bg-surface-2"
          >
            Cancel
          </button>
          <button
            onClick={shoot}
            disabled={!!error}
            className="rounded-lg bg-flight px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            Capture
          </button>
        </div>
      </div>
    </div>
  );
}

function MenuRow({
  children,
  onClick,
  icon,
}: {
  children: React.ReactNode;
  onClick: () => void;
  icon: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-ink transition-colors hover:bg-surface-2"
    >
      <span className="text-ink-faint">{icon}</span>
      {children}
    </button>
  );
}

/* ---- model picker ------------------------------------------------------- */

function ModelPicker({ model, onChange }: { model: string; onChange: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<ModelInfo[]>([]);

  useEffect(() => {
    if (open && models.length === 0) api.models().then((d) => setModels(d.models)).catch(() => {});
  }, [open, models.length]);

  const label = models.find((m) => m.id === model)?.label ?? model ?? "model";

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-full border border-edge px-2.5 py-1 text-xs text-ink-soft transition-colors hover:bg-surface-2"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-flight" />
        <span className="max-w-[9rem] truncate">{label}</span>
        <svg viewBox="0 0 12 12" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.4">
          <path d="M3 4.5 6 7.5 9 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full right-0 z-20 mb-2 max-h-72 w-60 overflow-y-auto rounded-xl border border-edge bg-surface py-1 shadow-composer">
            {models.length === 0 && (
              <p className="px-3 py-2 text-xs text-ink-faint">Loading models…</p>
            )}
            {models.map((m) => (
              <button
                key={m.id}
                onClick={() => {
                  onChange(m.id);
                  setOpen(false);
                }}
                className={`flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm transition-colors hover:bg-surface-2 ${
                  m.id === model ? "text-flight" : "text-ink"
                }`}
              >
                <span className="truncate">{m.label}</span>
                {m.id === model && <span className="text-xs">✓</span>}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/* ---- icons -------------------------------------------------------------- */

const PlusIcon = () => (
  <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7">
    <path d="M10 4v12M4 10h12" strokeLinecap="round" />
  </svg>
);
const SendIcon = () => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M10 16V4M5 9l5-5 5 5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const MicIcon = () => (
  <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="7.5" y="2.5" width="5" height="9" rx="2.5" />
    <path d="M4.5 9a5.5 5.5 0 0 0 11 0M10 14.5V17" strokeLinecap="round" />
  </svg>
);
const WaveIcon = () => (
  <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 10h1.5M7 6v8M10 3.5v13M13 6v8M16.5 10H18" strokeLinecap="round" />
  </svg>
);
const ImageIcon = () => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="3" y="4" width="14" height="12" rx="2" />
    <circle cx="7.5" cy="8.5" r="1.5" />
    <path d="M4 14l4-3 3 2 3-3 2 2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const CameraIcon = () => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 7a2 2 0 0 1 2-2h1l1-1.5h6L14 5h1a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" strokeLinejoin="round" />
    <circle cx="10" cy="10.5" r="2.8" />
  </svg>
);
const GithubIcon = () => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="currentColor" aria-hidden>
    <path d="M10 1.5a8.5 8.5 0 0 0-2.7 16.6c.4.1.6-.2.6-.4v-1.5c-2.3.5-2.8-1-2.8-1-.4-1-1-1.3-1-1.3-.8-.5 0-.5 0-.5.9.1 1.3.9 1.3.9.8 1.3 2 .9 2.5.7.1-.6.3-.9.5-1.1-1.8-.2-3.7-.9-3.7-4a3.1 3.1 0 0 1 .8-2.2c-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8a7.6 7.6 0 0 1 4 0c1.5-1 2.2-.8 2.2-.8.5 1.1.2 1.9.1 2.1a3.1 3.1 0 0 1 .8 2.2c0 3.1-1.9 3.8-3.7 4 .3.3.6.8.6 1.6v2.4c0 .2.1.5.6.4A8.5 8.5 0 0 0 10 1.5z" />
  </svg>
);
