import React, { useEffect, useMemo, useState } from 'react';
import { Cross2Icon } from '@radix-ui/react-icons';
import {
  listEducoreMappingPairs,
  type EducoreMappingPair,
} from '../../../../services/educoreService';
import { useToast } from '../../../../context/ToastContext';
import { errorToString } from '../../../../utils/errorUtils';
import './EducoreStandardsDrawer.css';

export interface EducoreMappingsDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the chosen pair; the parent loads + navigates to its group. */
  onSelect: (pair: EducoreMappingPair) => void;
  /** Key of the pair currently being loaded ("srcId-tgtId"), to show a spinner. */
  loadingPairKey?: string | null;
}

const pairKey = (p: EducoreMappingPair) =>
  `${p.sourceDataModelId}-${p.targetDataModelId}`;

/**
 * Slide-out that lists the directed EDUcore spec pairs (both specs imported)
 * which have a canonical MAPS_TO crosswalk ready to load into a mapping group.
 */
const EducoreMappingsDrawer: React.FC<EducoreMappingsDrawerProps> = ({
  open,
  onOpenChange,
  onSelect,
  loadingPairKey,
}) => {
  const { showToast } = useToast();
  const [loading, setLoading] = useState(false);
  const [pairs, setPairs] = useState<EducoreMappingPair[]>([]);
  const [filter, setFilter] = useState('');

  // Load the available pairs each time the drawer opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setFilter('');
    setLoading(true);
    (async () => {
      try {
        const data = await listEducoreMappingPairs();
        if (!cancelled) setPairs(data);
      } catch (e) {
        if (!cancelled) {
          showToast(errorToString(e), 'error');
          setPairs([]);
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
    return pairs.filter(
      (p) =>
        !q ||
        `${p.sourceShort} ${p.targetShort}`.toLowerCase().includes(q)
    );
  }, [pairs, filter]);

  if (!open) return null;

  return (
    <div role="dialog" aria-modal="true" aria-label="Load EDUcore mapping pairs">
      <div
        className="educore-drawer-overlay"
        onClick={() => onOpenChange(false)}
      />
      <div className="educore-drawer-content">
        <div className="educore-drawer-header">
          <h2 className="educore-drawer-title">Load EDUcore mappings</h2>
          <button
            type="button"
            className="educore-drawer-close"
            aria-label="Close"
            title="Close"
            onClick={() => onOpenChange(false)}
          >
            <Cross2Icon width={18} height={18} />
          </button>
        </div>

        <div className="educore-drawer-search">
          <input
            type="text"
            value={filter}
            placeholder="Filter pairs…"
            aria-label="Filter mapping pairs"
            autoFocus
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        <div className="educore-drawer-list">
          {loading ? (
            <div className="educore-drawer-loading">
              <span className="educore-drawer-spinner" aria-hidden="true" />{' '}
              Finding available mappings…
            </div>
          ) : filtered.length === 0 ? (
            <div className="educore-drawer-empty">
              {pairs.length === 0
                ? 'No mapping pairs available. Import both standards of a pair first (globe icon).'
                : 'No pairs match your filter.'}
            </div>
          ) : (
            filtered.map((pair) => {
              const isLoading = loadingPairKey === pairKey(pair);
              return (
                <button
                  key={pairKey(pair)}
                  type="button"
                  className="educore-drawer-item"
                  disabled={!!loadingPairKey}
                  title={`Load the canonical ${pair.sourceShort} → ${pair.targetShort} mappings`}
                  onClick={() => onSelect(pair)}
                >
                  <div className="educore-drawer-item-title-row">
                    <span className="educore-drawer-item-title">
                      {pair.sourceShort} → {pair.targetShort}
                    </span>
                    {isLoading ? (
                      <span
                        className="educore-drawer-spinner"
                        aria-label="Loading"
                      />
                    ) : (
                      <span className="educore-drawer-item-version">
                        {pair.mappingCount.toLocaleString()} mappings
                      </span>
                    )}
                  </div>
                </button>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

export default EducoreMappingsDrawer;
