import React, { useLayoutEffect, useRef, useState } from 'react';
import type { SuggestedMappingWire } from '../hooks/useSuggestedMappings';

export interface SuggestionPopoverProps {
    suggestion: SuggestedMappingWire;
    /** Source field display name (Entity.Attribute) */
    sourceName: string;
    /** Target field display name (Entity.Attribute) */
    targetName: string;
    /** Position relative to the wires container */
    position: { x: number; y: number };
    onConfirm: (suggestionId: string) => void;
    onReject: (suggestionId: string) => void;
}

const GAP = 8;
const EDGE_PAD = 6;

const SuggestionPopover: React.FC<SuggestionPopoverProps> = ({
    suggestion,
    sourceName,
    targetName,
    position,
    onConfirm,
    onReject,
}) => {
    const confidencePercent = Math.round(suggestion.confidence * 100);
    const ref = useRef<HTMLDivElement | null>(null);
    // flipBelow: place popover under the click instead of above it.
    // dx: horizontal correction (px) added to the centered transform so the popover
    //     stays inside the offsetParent (the wires slot) on left/right edges.
    const [adjust, setAdjust] = useState<{ flipBelow: boolean; dx: number }>({
        flipBelow: false,
        dx: 0,
    });

    useLayoutEffect(() => {
        const el = ref.current;
        if (!el) return;
        const parent = el.offsetParent as HTMLElement | null;
        if (!parent) return;
        const popRect = el.getBoundingClientRect();
        const parentRect = parent.getBoundingClientRect();

        const flipBelow = popRect.top < parentRect.top + EDGE_PAD;

        let dx = 0;
        if (popRect.left < parentRect.left + EDGE_PAD) {
            dx = parentRect.left + EDGE_PAD - popRect.left;
        } else if (popRect.right > parentRect.right - EDGE_PAD) {
            dx = parentRect.right - EDGE_PAD - popRect.right;
        }

        if (flipBelow !== adjust.flipBelow || dx !== adjust.dx) {
            setAdjust({ flipBelow, dx });
        }
    }, [position.x, position.y, suggestion.id, adjust.flipBelow, adjust.dx]);

    const transform = adjust.flipBelow
        ? `translate(calc(-50% + ${adjust.dx}px), ${GAP}px)`
        : `translate(calc(-50% + ${adjust.dx}px), -100%) translateY(-${GAP}px)`;

    return (
        <div
            ref={ref}
            className="suggestion-popover"
            style={{
                position: 'absolute',
                left: position.x,
                top: position.y,
                zIndex: 100,
                transform,
            }}
            onClick={(e) => e.stopPropagation()}
        >
            <div className="suggestion-popover__header">
                <span className="suggestion-popover__confidence">
                    {confidencePercent}%
                </span>
                <span className="suggestion-popover__mapping">
                    {sourceName} &rarr; {targetName}
                </span>
            </div>
            <div className="suggestion-popover__reason">{suggestion.reason}</div>
            <div className="suggestion-popover__actions">
                <button
                    type="button"
                    className="suggestion-popover__btn suggestion-popover__btn--confirm"
                    onClick={() => onConfirm(suggestion.id)}
                    title="Confirm mapping"
                >
                    &#x2713;
                </button>
                <button
                    type="button"
                    className="suggestion-popover__btn suggestion-popover__btn--reject"
                    onClick={() => onReject(suggestion.id)}
                    title="Reject suggestion"
                >
                    &#x2717;
                </button>
            </div>
        </div>
    );
};

export default SuggestionPopover;
