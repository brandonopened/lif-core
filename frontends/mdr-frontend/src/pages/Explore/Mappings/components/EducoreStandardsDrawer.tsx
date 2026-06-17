import React, { useEffect, useMemo, useState } from 'react';
import { Cross2Icon } from '@radix-ui/react-icons';
import {
  listEducoreStandards,
  importEducoreStandard,
  type EducoreStandard,
} from '../../../../services/educoreService';
import { useToast } from '../../../../context/ToastContext';
import { errorToString } from '../../../../utils/errorUtils';
import './EducoreStandardsDrawer.css';

export interface EducoreStandardsDrawerProps {
  open: boolean;
  side: 'source' | 'target';
  onOpenChange: (open: boolean) => void;
  onSelect: (modelId: number, name: string) => void;
}

const EducoreStandardsDrawer: React.FC<EducoreStandardsDrawerProps> = ({
  open,
  side,
  onOpenChange,
  onSelect,
}) => {
  const { showToast } = useToast();
  const [loading, setLoading] = useState(false);
  const [standards, setStandards] = useState<EducoreStandard[]>([]);
  const [filter, setFilter] = useState('');
  const [importingKey, setImportingKey] = useState<string | null>(null);

  // Load the standards each time the drawer opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setFilter('');
    setImportingKey(null);
    setLoading(true);
    (async () => {
      try {
        const data = await listEducoreStandards();
        if (!cancelled) setStandards(data);
      } catch (e) {
        if (!cancelled) {
          showToast(errorToString(e), 'error');
          setStandards([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, showToast]);

  // Close on Escape for keyboard accessibility.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onOpenChange(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onOpenChange]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const matches = (s: EducoreStandard) =>
      !q ||
      (s.displayName || '').toLowerCase().includes(q) ||
      (s.title || '').toLowerCase().includes(q);
    // Importable standards first, then alphabetically by short label.
    return standards
      .filter(matches)
      .slice()
      .sort((a, b) => {
        const ai = a.importable !== false ? 0 : 1;
        const bi = b.importable !== false ? 0 : 1;
        if (ai !== bi) return ai - bi;
        return (a.displayName || a.title || '').localeCompare(
          b.displayName || b.title || ''
        );
      });
  }, [standards, filter]);

  const handleSelect = async (standard: EducoreStandard) => {
    if (importingKey || standard.importable === false) return;
    setImportingKey(standard.key);
    try {
      const result = await importEducoreStandard(standard.key);
      onSelect(result.Id, result.Name);
      onOpenChange(false);
    } catch (e) {
      showToast(errorToString(e), 'error');
    } finally {
      setImportingKey(null);
    }
  };

  if (!open) return null;

  const sideLabel = side === 'target' ? 'Target' : 'Source';

  return (
    <div role="dialog" aria-modal="true" aria-label={`Load EDUcore standard as ${sideLabel}`}>
      <div
        className="educore-drawer-overlay"
        onClick={() => !importingKey && onOpenChange(false)}
      />
      <div className="educore-drawer-content">
        <div className="educore-drawer-header">
          <h2 className="educore-drawer-title">Load EDUcore standard as {sideLabel}</h2>
          <button
            type="button"
            className="educore-drawer-close"
            aria-label="Close"
            title="Close"
            disabled={!!importingKey}
            onClick={() => onOpenChange(false)}
          >
            <Cross2Icon width={18} height={18} />
          </button>
        </div>

        <div className="educore-drawer-search">
          <input
            type="text"
            value={filter}
            placeholder="Filter standards by title…"
            aria-label="Filter standards by title"
            autoFocus
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        <div className="educore-drawer-list">
          {loading ? (
            <div className="educore-drawer-loading">
              <span className="educore-drawer-spinner" aria-hidden="true" /> Loading standards…
            </div>
          ) : filtered.length === 0 ? (
            <div className="educore-drawer-empty">
              {standards.length === 0
                ? 'No standards available.'
                : 'No standards match your filter.'}
            </div>
          ) : (
            filtered.map((standard) => {
              const isImporting = importingKey === standard.key;
              const importable = standard.importable !== false;
              const label = standard.displayName || standard.title;
              const showFullTitle =
                !!standard.title && standard.title !== label;
              return (
                <button
                  key={standard.key}
                  type="button"
                  className={
                    'educore-drawer-item' +
                    (importable ? '' : ' educore-drawer-item--disabled')
                  }
                  disabled={!!importingKey || !importable}
                  title={
                    importable
                      ? `Load "${standard.title}" as ${sideLabel.toLowerCase()}`
                      : 'A loader is not yet available for this standard'
                  }
                  onClick={() => handleSelect(standard)}
                >
                  <div className="educore-drawer-item-title-row">
                    <span className="educore-drawer-item-title">{label}</span>
                    {isImporting ? (
                      <span className="educore-drawer-spinner" aria-label="Importing" />
                    ) : !importable ? (
                      <span className="educore-drawer-item-tag">Coming soon</span>
                    ) : (
                      standard.version && (
                        <span className="educore-drawer-item-version">v{standard.version}</span>
                      )
                    )}
                  </div>
                  {showFullTitle && (
                    <span className="educore-drawer-item-subtitle">{standard.title}</span>
                  )}
                  {standard.description && (
                    <span className="educore-drawer-item-desc">{standard.description}</span>
                  )}
                </button>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

export default EducoreStandardsDrawer;
