import React, { useState } from 'react';
import { Copy, Check, Code } from 'lucide-react';

interface MarkdownRendererProps {
  content: string;
}

interface CodeBlockProps {
  language: string;
  code: string;
}

const CodeBlock: React.FC<CodeBlockProps> = ({ language, code }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="code-block-container">
      <div className="code-block-header">
        <div className="code-lang-tag">
          <Code size={13} />
          <span>{language || 'code'}</span>
        </div>
        <button className="code-copy-btn" onClick={handleCopy} title="Copy code">
          {copied ? (
            <>
              <Check size={13} className="text-success" />
              <span>Copied!</span>
            </>
          ) : (
            <>
              <Copy size={13} />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <pre className="code-block-pre">
        <code>{code}</code>
      </pre>
    </div>
  );
};

export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({ content }) => {
  if (!content) return null;

  // Split content by code blocks ```lang ... ```
  const codeBlockRegex = /```(\w*)\n([\s\S]*?)```/g;
  const elements: React.ReactNode[] = [];
  let lastIndex = 0;
  let match;

  while ((match = codeBlockRegex.exec(content)) !== null) {
    const textBefore = content.substring(lastIndex, match.index);
    if (textBefore) {
      elements.push(
        <React.Fragment key={`text-${lastIndex}`}>
          {renderFormattedText(textBefore)}
        </React.Fragment>
      );
    }

    const language = match[1] || 'text';
    const code = match[2];
    elements.push(
      <CodeBlock
        key={`code-${match.index}`}
        language={language}
        code={code.replace(/\n$/, '')}
      />
    );

    lastIndex = match.index + match[0].length;
  }

  const remainingText = content.substring(lastIndex);
  if (remainingText) {
    elements.push(
      <React.Fragment key={`text-${lastIndex}`}>
        {renderFormattedText(remainingText)}
      </React.Fragment>
    );
  }

  return <div className="markdown-body">{elements}</div>;
};

function renderFormattedText(text: string): React.ReactNode[] {
  const lines = text.split('\n');
  const rendered: React.ReactNode[] = [];
  let currentList: { ordered: boolean; items: string[] } | null = null;
  let inTable = false;
  let tableRows: string[][] = [];

  const flushList = (key: string) => {
    if (!currentList) return;
    const ListTag = currentList.ordered ? 'ol' : 'ul';
    rendered.push(
      <ListTag key={key} className="md-list">
        {currentList.items.map((item, idx) => (
          <li key={idx}>{renderInlineFormatting(item)}</li>
        ))}
      </ListTag>
    );
    currentList = null;
  };

  const flushTable = (key: string) => {
    if (!inTable || tableRows.length === 0) return;
    const header = tableRows[0];
    const body = tableRows.slice(1).filter((r) => !r.every((c) => /^[-:| ]+$/.test(c)));

    rendered.push(
      <div key={key} className="table-wrapper">
        <table className="md-table">
          <thead>
            <tr>
              {header.map((col, idx) => (
                <th key={idx}>{renderInlineFormatting(col.trim())}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, rIdx) => (
              <tr key={rIdx}>
                {row.map((cell, cIdx) => (
                  <td key={cIdx}>{renderInlineFormatting(cell.trim())}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
    tableRows = [];
    inTable = false;
  };

  lines.forEach((line, index) => {
    const trimmed = line.trim();

    // Table detection
    if (trimmed.startsWith('|') && trimmed.endsWith('|')) {
      flushList(`flush-list-${index}`);
      inTable = true;
      const cells = trimmed
        .slice(1, -1)
        .split('|')
        .map((c) => c.trim());
      tableRows.push(cells);
      return;
    } else if (inTable) {
      flushTable(`flush-table-${index}`);
    }

    // Unordered List
    if (/^[-*]\s+/.test(trimmed)) {
      const itemText = trimmed.replace(/^[-*]\s+/, '');
      if (currentList && !currentList.ordered) {
        currentList.items.push(itemText);
      } else {
        flushList(`flush-ordered-${index}`);
        currentList = { ordered: false, items: [itemText] };
      }
      return;
    }

    // Ordered List
    if (/^\d+\.\s+/.test(trimmed)) {
      const itemText = trimmed.replace(/^\d+\.\s+/, '');
      if (currentList && currentList.ordered) {
        currentList.items.push(itemText);
      } else {
        flushList(`flush-unordered-${index}`);
        currentList = { ordered: true, items: [itemText] };
      }
      return;
    }

    flushList(`flush-list-${index}`);

    // Headings
    if (trimmed.startsWith('### ')) {
      rendered.push(
        <h4 key={index} className="md-h3">
          {renderInlineFormatting(trimmed.substring(4))}
        </h4>
      );
      return;
    }
    if (trimmed.startsWith('## ')) {
      rendered.push(
        <h3 key={index} className="md-h2">
          {renderInlineFormatting(trimmed.substring(3))}
        </h3>
      );
      return;
    }
    if (trimmed.startsWith('# ')) {
      rendered.push(
        <h2 key={index} className="md-h1">
          {renderInlineFormatting(trimmed.substring(2))}
        </h2>
      );
      return;
    }

    // Blockquote
    if (trimmed.startsWith('> ')) {
      rendered.push(
        <blockquote key={index} className="md-blockquote">
          {renderInlineFormatting(trimmed.substring(2))}
        </blockquote>
      );
      return;
    }

    // Empty lines
    if (!trimmed) {
      rendered.push(<div key={index} className="md-spacer" />);
      return;
    }

    // Normal Paragraph
    rendered.push(
      <p key={index} className="md-paragraph">
        {renderInlineFormatting(line)}
      </p>
    );
  });

  flushList('final-list');
  flushTable('final-table');

  return rendered;
}

function renderInlineFormatting(text: string): React.ReactNode {
  // Bold: **text**
  // Inline code: `code`
  // Italic: *text*
  const parts: React.ReactNode[] = [];
  let buffer = '';
  let i = 0;

  while (i < text.length) {
    // Inline code: `code`
    if (text[i] === '`') {
      if (buffer) {
        parts.push(buffer);
        buffer = '';
      }
      const closeIdx = text.indexOf('`', i + 1);
      if (closeIdx !== -1) {
        const codeText = text.substring(i + 1, closeIdx);
        parts.push(
          <code key={`code-${i}`} className="md-inline-code">
            {codeText}
          </code>
        );
        i = closeIdx + 1;
        continue;
      }
    }

    // Bold: **text**
    if (text[i] === '*' && text[i + 1] === '*') {
      if (buffer) {
        parts.push(buffer);
        buffer = '';
      }
      const closeIdx = text.indexOf('**', i + 2);
      if (closeIdx !== -1) {
        const boldText = text.substring(i + 2, closeIdx);
        parts.push(
          <strong key={`bold-${i}`} className="md-bold">
            {boldText}
          </strong>
        );
        i = closeIdx + 2;
        continue;
      }
    }

    buffer += text[i];
    i++;
  }

  if (buffer) {
    parts.push(buffer);
  }

  return parts.length === 1 ? parts[0] : <>{parts}</>;
}
