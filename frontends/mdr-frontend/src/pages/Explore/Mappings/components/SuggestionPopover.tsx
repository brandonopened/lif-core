import React from 'react';
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

const SuggestionPopover: React.FC<SuggestionPopoverProps> = ({
    suggestion,
    sourceName,
    targetName,
    position,
    onConfirm,
    onReject,
}) => {
    const confidencePercent = Math.round(suggestion.confidence * 100);

    return (
        <div
            className="suggestion-popover"
            style={{
                position: 'absolute',
                left: position.x,
                top: position.y,
                zIndex: 100,
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
