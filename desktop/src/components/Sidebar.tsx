import React, { useState } from 'react';
import { Conversation, AppState } from '../types';
import { MessageSquare, Plus, Trash2, Settings, ShieldCheck, Cpu, Edit2, Check, Activity, Search } from 'lucide-react';

interface SidebarProps {
  conversations: Conversation[];
  activeConversationId: string | null;
  appState: AppState;
  onSelectConversation: (id: string) => void;
  onNewConversation: () => void;
  onDeleteConversation: (id: string) => void;
  onRenameConversation: (id: string, newTitle: string) => void;
  onOpenSettings: () => void;
  onOpenLoader: () => void;
  onOpenDashboard: () => void;
  appVersion: string;
}

export const Sidebar: React.FC<SidebarProps> = ({
  conversations,
  activeConversationId,
  appState,
  onSelectConversation,
  onNewConversation,
  onDeleteConversation,
  onRenameConversation,
  onOpenSettings,
  onOpenLoader,
  onOpenDashboard,
  appVersion,
}) => {
  const [search, setSearch] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');

  const filteredConversations = conversations.filter((c) =>
    (c.title || 'Untitled Chat').toLowerCase().includes(search.toLowerCase())
  );

  const startRename = (c: Conversation) => {
    setEditingId(c.id);
    setEditTitle(c.title || 'Untitled Chat');
  };

  const saveRename = (id: string) => {
    if (editTitle.trim()) {
      onRenameConversation(id, editTitle.trim());
    }
    setEditingId(null);
  };

  const getStatusBadge = () => {
    switch (appState) {
      case 'MODEL_READY':
        return <span className="status-chip ready"><span className="dot ready" />Ready</span>;
      case 'MODEL_STARTING':
      case 'PREFLIGHT_RUNNING':
        return <span className="status-chip starting"><span className="dot starting" />Starting</span>;
      case 'LOW_MEMORY_WAIT':
        return <span className="status-chip wait"><span className="dot wait" />Recovery</span>;
      case 'GENERATING':
        return <span className="status-chip gen"><span className="dot gen" />Inferring</span>;
      default:
        return <span className="status-chip unloaded"><span className="dot unloaded" />Standby</span>;
    }
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="brand-row">
          <div className="brand-logo">
            <Cpu className="brand-icon" size={22} />
            <div className="brand-titles">
              <span className="brand-name">NOVA Chat</span>
              <span className="brand-sub">Local MoE Engine</span>
            </div>
          </div>
          {getStatusBadge()}
        </div>

        <button className="btn-new-chat" onClick={onNewConversation}>
          <Plus size={16} />
          <span>New Chat</span>
        </button>

        {conversations.length > 1 && (
          <div className="search-input-wrapper">
            <Search size={14} className="search-icon" />
            <input
              type="text"
              className="sidebar-search"
              placeholder="Search conversations..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        )}
      </div>

      <div className="conversation-list">
        {filteredConversations.length === 0 ? (
          <div className="empty-history">
            <span>{search ? 'No matching conversations' : 'No conversations yet'}</span>
          </div>
        ) : (
          filteredConversations.map((c) => {
            const isActive = c.id === activeConversationId;
            const isEditing = c.id === editingId;

            return (
              <div
                key={c.id}
                className={`conversation-item ${isActive ? 'active' : ''}`}
                onClick={() => onSelectConversation(c.id)}
              >
                <MessageSquare size={15} className="conv-icon" />

                {isEditing ? (
                  <div className="conv-edit-box" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="text"
                      className="conv-rename-input"
                      value={editTitle}
                      onChange={(e) => setEditTitle(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveRename(c.id);
                        if (e.key === 'Escape') setEditingId(null);
                      }}
                      autoFocus
                    />
                    <button
                      className="conv-action-btn"
                      onClick={() => saveRename(c.id)}
                      title="Save"
                    >
                      <Check size={13} />
                    </button>
                  </div>
                ) : (
                  <>
                    <span
                      className="conv-title"
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        startRename(c);
                      }}
                    >
                      {c.title || 'Untitled Chat'}
                    </span>
                    <div className="conv-hover-actions">
                      <button
                        className="conv-action-btn"
                        title="Rename"
                        onClick={(e) => {
                          e.stopPropagation();
                          startRename(c);
                        }}
                      >
                        <Edit2 size={12} />
                      </button>
                      <button
                        className="conv-action-btn delete-btn"
                        title="Delete chat"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteConversation(c.id);
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </>
                )}
              </div>
            );
          })
        )}
      </div>

      <div className="sidebar-footer">
        <button className="footer-btn loader-btn" onClick={onOpenLoader}>
          <ShieldCheck size={16} />
          <span>Resource Recovery & Loader</span>
        </button>

        <button className="footer-btn telemetry-btn-side" onClick={onOpenDashboard}>
          <Activity size={16} />
          <span>Telemetry Dashboard</span>
        </button>

        <div className="footer-bottom-row">
          <button className="footer-icon-btn" title="Settings" onClick={onOpenSettings}>
            <Settings size={16} />
            <span>Settings</span>
          </button>
          <span className="app-version-label">v{appVersion}</span>
        </div>
      </div>
    </aside>
  );
};
