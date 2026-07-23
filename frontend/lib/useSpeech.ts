"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/*
 * Minimal typings for the Web Speech API, which ships in Chromium browsers but
 * is absent from the DOM lib. Only the surface this hook touches is declared.
 */
interface SpeechRecognitionAlternative {
  transcript: string;
}
interface SpeechRecognitionResult {
  0: SpeechRecognitionAlternative;
  isFinal: boolean;
}
interface SpeechRecognitionResultList {
  length: number;
  [i: number]: SpeechRecognitionResult;
}
interface SpeechRecognitionEvent {
  resultIndex: number;
  results: SpeechRecognitionResultList;
}
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: SpeechRecognitionEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
}

type Recognizer = new () => SpeechRecognitionLike;

function getRecognizer(): Recognizer | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: Recognizer;
    webkitSpeechRecognition?: Recognizer;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

/**
 * Speech-to-text over the Web Speech API, used for both composer voice buttons:
 * `continuous: true` powers the dictation toggle, `false` the hold-to-talk mic.
 * Each finalised phrase is handed to `onText` to append to the draft.
 *
 * `supported` is false where the API is missing (Firefox, older Safari), so the
 * caller can disable the buttons rather than have them silently do nothing.
 */
export function useSpeech(onText: (text: string) => void) {
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  useEffect(() => setSupported(getRecognizer() !== null), []);

  const stop = useCallback(() => {
    recognitionRef.current?.stop();
  }, []);

  const start = useCallback((continuous: boolean) => {
    const Recognizer = getRecognizer();
    if (!Recognizer) return;

    // Tear down any previous session so the two modes never run at once.
    recognitionRef.current?.abort();

    const recognition = new Recognizer();
    recognition.lang =
      (typeof navigator !== "undefined" && navigator.language) || "en-US";
    recognition.continuous = continuous;
    recognition.interimResults = false;

    recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          const text = result[0].transcript.trim();
          if (text) onTextRef.current(text);
        }
      }
    };
    recognition.onend = () => setListening(false);
    recognition.onerror = () => setListening(false);

    recognitionRef.current = recognition;
    recognition.start();
    setListening(true);
  }, []);

  useEffect(() => () => recognitionRef.current?.abort(), []);

  return { supported, listening, start, stop };
}
