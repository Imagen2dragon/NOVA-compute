import React, { useEffect, useRef, useState } from 'react';
import { ChatMessage, AppState } from '../types';
import { Cpu, User, Copy, Check, ShieldAlert, Play, ArrowDown, RotateCcw } from 'lucide-react';
import { MarkdownRenderer } from './MarkdownRenderer';

interface ChatAreaProps {
  messages: ChatMessage[];
  appState: AppState;
  onOpenLoader: () => void;
  onQuickPrompt?: (prompt: string) => void;
  onRegenerate?: () => void;
}

export const ChatArea: React.FC<ChatAreaProps> = ({
  messages,
  appState,
  onOpenLoader,
  onQuickPrompt,
  onRegenerate,
}) => {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [showScrollBottom, setShowScrollBottom] = useState(false);

  const isModelReady = appState === 'MODEL_READY' || appState === 'GENERATING';
  const isGenerating = appState === 'GENERATING';

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const isUp = scrollHeight - scrollTop - clientHeight > 120;
    setShowScrollBottom(isUp);
  };

  const scrollToBottom = () => {
    if (scrollRef.current) {
      scrollRef.current.scrollTo({
        top: scrollRef.current.scrollHeight,
        behavior: 'smooth',
      });
    }
  };

  const handleCopy = (id: string, text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  if (messages.length === 0) {
    return (
      <div className="chat-area empty-state-container" ref={scrollRef}>
        <div className="welcome-card">
          <div className="welcome-icon-glow">
            <Cpu size={40} className="welcome-icon" />
          </div>
          <h2 className="welcome-title">NOVA Chat Workstation</h2>
          <p className="welcome-description">
            Resource-aware, private local AI running on your RTX 4050 laptop hardware.
            Powered by Qwen3-30B-A3B MoE with adaptive memory offloading and Win32 Job Object containment.
          </p>

          {!isModelReady && (
            <div className="welcome-safety-notice">
              <ShieldAlert size={20} className="notice-icon" />
              <div className="notice-content">
                <span className="notice-heading">Model Unloaded (Zero Memory Footprint)</span>
                <span className="notice-body">
                  To protect host responsiveness, NOVA Chat starts immediately without loading weights.
                  Open the Guided Loader below to qualify your hardware and select a runtime profile.
                </span>
                <button className="welcome-load-btn" onClick={onOpenLoader}>
                  <Play size={14} />
                  <span>Open Resource Recovery & Model Loader</span>
                </button>
              </div>
            </div>
          )}

          <div className="quick-starters">
            <span className="quick-starter-label">Example inquiries:</span>
            <div className="starter-chips">
              <button
                className="starter-chip"
                onClick={() =>
                  onQuickPrompt?.('Explain how Mixture of Experts (MoE) coordinates parameter routing.')
                }
              >
                "Explain how Mixture of Experts coordinates routing"
              </button>
              <button
                className="starter-chip"
                onClick={() =>
                  onQuickPrompt?.('Write a concise Rust function to query memory counters safely via windows-sys.')
                }
              >
                "Write a Rust function to query memory counters via windows-sys"
              </button>
              <button
                className="starter-chip"
                onClick={() =>
                  onQuickPrompt?.('What is the difference between resident working set and private commit?')
                }
              >
                "Difference between resident working set and private commit"
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-area" ref={scrollRef} onScroll={handleScroll}>
      <div className="messages-stream">
        {messages.map((m, idx) => {
          const isUser = m.role === 'user';
          const isStreaming = m.status === 'streaming';
          const isError = m.status === 'error';
          const isLastAssistant = !isUser && idx === messages.length - 1;

          return (
            <div
              key={m.id}
              className={`message-row ${isUser ? 'user-row' : 'assistant-row'} ${isError ? 'error-row' : ''}`}
            >
              <div className="message-avatar">
                {isUser ? <User size={18} /> : <Cpu size={18} />}
              </div>

              <div className="message-content-wrapper">
                <div className="message-sender-bar">
                  <span className="sender-name">{isUser ? 'You' : 'NOVA Compute'}</span>
                  <span className="message-time">
                    {new Date(m.timestamp).toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>
                  {!isUser && m.content && (
                    <div className="message-actions-cluster">
                      <button
                        className="copy-bubble-btn"
                        title="Copy response"
                        onClick={() => handleCopy(m.id, m.content)}
                      >
                        {copiedId === m.id ? <Check size={13} className="text-success" /> : <Copy size={13} />}
                      </button>
                      {isLastAssistant && !isGenerating && onRegenerate && (
                        <button
                          className="copy-bubble-btn"
                          title="Regenerate response"
                          onClick={onRegenerate}
                        >
                          <RotateCcw size={13} />
                        </button>
                      )}
                    </div>
                  )}
                </div>

                <div className="message-body">
                  {isUser ? (
                    <div className="user-message-text">{m.content}</div>
                  ) : (
                    <>
                      <MarkdownRenderer content={m.content} />
                      {isStreaming && <span className="streaming-cursor">█</span>}
                    </>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {showScrollBottom && (
        <button
          className="btn-scroll-bottom"
          onClick={scrollToBottom}
          title="Scroll to bottom"
        >
          <ArrowDown size={16} />
        </button>
      )}
    </div>
  );
};
