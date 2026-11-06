import React, { useState, useRef, useEffect } from 'react';
import { AppState } from '../types';
import { ArrowUp, Square, ShieldAlert } from 'lucide-react';

interface ComposerProps {
  appState: AppState;
  onSendMessage: (text: string) => void;
  onStopGeneration: () => void;
  onOpenLoader: () => void;
}

export const Composer: React.FC<ComposerProps> = ({
  appState,
  onSendMessage,
  onStopGeneration,
  onOpenLoader,
}) => {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isGenerating = appState === 'GENERATING';
  const isModelReady = appState === 'MODEL_READY';
  const isUnloaded = !isModelReady && !isGenerating;

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 180)}px`;
    }
  }, [text]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (isUnloaded) {
      onOpenLoader();
      return;
    }
    onSendMessage(trimmed);
    setText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  return (
    <div className="composer-container">
      {isUnloaded && (
        <div className="composer-unloaded-advisory" onClick={onOpenLoader}>
          <ShieldAlert size={15} />
          <span>Model is currently unloaded. Click here to open Guided Model Loader and verify hardware safety.</span>
        </div>
      )}

      <div className={`composer-box ${isUnloaded ? 'disabled' : ''}`}>
        <textarea
          ref={textareaRef}
          className="composer-textarea"
          placeholder={
            isUnloaded
              ? 'Model is unloaded. Open Guided Model Loader above to initialize...'
              : isGenerating
              ? 'NOVA is generating response...'
              : 'Message NOVA Compute (Shift+Enter for newline)...'
          }
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={isGenerating}
        />

        <div className="composer-controls">
          {isGenerating ? (
            <button
              className="composer-action-btn stop-btn"
              onClick={onStopGeneration}
              title="Stop generation"
            >
              <Square size={16} />
              <span>Stop</span>
            </button>
          ) : (
            <button
              className={`composer-action-btn send-btn ${text.trim() ? 'active' : ''}`}
              onClick={handleSend}
              disabled={!text.trim()}
              title={isUnloaded ? 'Open Guided Loader' : 'Send message'}
            >
              <ArrowUp size={18} />
            </button>
          )}
        </div>
      </div>
      <div className="composer-footer-note">
        <span>NOVA Local Engine • Context: 512 tokens • Zero telemetry leaving your machine</span>
      </div>
    </div>
  );
};
