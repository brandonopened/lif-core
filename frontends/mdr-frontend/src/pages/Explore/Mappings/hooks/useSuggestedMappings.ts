import { useState, useCallback, useRef } from 'react';
import {
    suggestMappings,
    FieldInfo,
    SuggestedMapping,
} from '../../../../services/suggestMappingsService';
import type {
    DataModelWithDetailsWithTree,
    AttributeDTO,
    EntityWithAttributesDTO,
} from '../../../../types';
import type { TransformationData } from '../../../../services/transformationsService';

export interface SuggestedMappingWire {
    id: string;
    sourceAttrId: number;
    sourceEntityIdPath: string | null;
    targetAttrId: number;
    targetEntityIdPath: string | null;
    confidence: number;
    reason: string;
}

interface UseSuggestedMappingsParams {
    sourceModel: DataModelWithDetailsWithTree | null;
    targetModel: DataModelWithDetailsWithTree | null;
    transformations: TransformationData[];
    groupId: number;
}

function buildFieldInfoFromModel(
    model: DataModelWithDetailsWithTree,
    attrId: number,
    entityIdPath: string | null,
): FieldInfo | null {
    for (const ewa of model.Entities) {
        const attr = ewa.Attributes.find((a: AttributeDTO) => a.Id === attrId);
        if (!attr) continue;

        let valueSetValues: string[] | null = null;
        if (attr.ValueSetId) {
            const vs = model.ValueSets.find(
                (v) => v.ValueSet.Id === attr.ValueSetId,
            );
            if (vs) {
                valueSetValues = vs.Values.slice(0, 20).map(
                    (v) => v.ValueName || v.Value,
                );
            }
        }

        return {
            attribute_id: attr.Id,
            attribute_name: attr.Name,
            entity_name: ewa.Entity.Name,
            entity_id_path: entityIdPath,
            description: attr.Description,
            data_type: attr.DataType,
            value_set_values: valueSetValues,
        };
    }
    return null;
}

function buildAllFieldInfoFromModel(
    model: DataModelWithDetailsWithTree,
): FieldInfo[] {
    const fields: FieldInfo[] = [];
    for (const ewa of model.Entities) {
        for (const attr of ewa.Attributes) {
            let valueSetValues: string[] | null = null;
            if (attr.ValueSetId) {
                const vs = model.ValueSets.find(
                    (v) => v.ValueSet.Id === attr.ValueSetId,
                );
                if (vs) {
                    valueSetValues = vs.Values.slice(0, 20).map(
                        (v) => v.ValueName || v.Value,
                    );
                }
            }
            fields.push({
                attribute_id: attr.Id,
                attribute_name: attr.Name,
                entity_name: ewa.Entity.Name,
                entity_id_path: null, // populated from tree if available
                description: attr.Description,
                data_type: attr.DataType,
                value_set_values: valueSetValues,
            });
        }
    }
    return fields;
}

export default function useSuggestedMappings({
    sourceModel,
    targetModel,
    transformations,
    groupId,
}: UseSuggestedMappingsParams) {
    const [suggestedMappings, setSuggestedMappings] = useState<
        SuggestedMappingWire[]
    >([]);
    const [loadingSuggestions, setLoadingSuggestions] = useState(false);
    const [activeFieldId, setActiveFieldId] = useState<number | null>(null);
    const requestIdRef = useRef(0);

    const fetchSuggestions = useCallback(
        async (
            side: 'source' | 'target',
            attrId: number,
            entityIdPath: string | null,
        ) => {
            if (groupId < 0 || !sourceModel || !targetModel) return;

            const selectedModel =
                side === 'source' ? sourceModel : targetModel;
            const candidateModel =
                side === 'source' ? targetModel : sourceModel;

            const selectedField = buildFieldInfoFromModel(
                selectedModel,
                attrId,
                entityIdPath,
            );
            if (!selectedField) return;

            // Build candidate fields from the opposite model
            let candidateFields = buildAllFieldInfoFromModel(candidateModel);

            // Filter out already-mapped fields
            const mappedTargetIds = new Set(
                transformations
                    .map((t) => t.TargetAttribute?.AttributeId)
                    .filter((id): id is number => typeof id === 'number'),
            );
            const mappedSourceIds = new Set(
                transformations.flatMap(
                    (t) =>
                        t.SourceAttributes?.map((s) => s.AttributeId).filter(
                            (id): id is number => typeof id === 'number',
                        ) ?? [],
                ),
            );

            if (side === 'source') {
                // Candidates are target fields; filter already-mapped targets
                candidateFields = candidateFields.filter(
                    (f) => !mappedTargetIds.has(f.attribute_id),
                );
            } else {
                // Candidates are source fields; filter already-mapped sources
                candidateFields = candidateFields.filter(
                    (f) => !mappedSourceIds.has(f.attribute_id),
                );
            }

            // Cap at 200 candidates
            candidateFields = candidateFields.slice(0, 200);

            if (candidateFields.length === 0) {
                setSuggestedMappings([]);
                return;
            }

            // Build existing mappings for context
            const existingMappings = transformations
                .filter((t) => t.SourceAttributes?.length && t.TargetAttribute)
                .map((t) => ({
                    source_name: `${t.SourceEntity?.Name || '?'}.${t.SourceAttributes?.[0]?.AttributeId || '?'}`,
                    target_name: `${t.TargetEntity?.Name || '?'}.${t.TargetAttribute?.AttributeName || '?'}`,
                }));

            const currentRequestId = ++requestIdRef.current;
            setLoadingSuggestions(true);
            setActiveFieldId(attrId);

            try {
                const results = await suggestMappings({
                    selected_side: side,
                    selected_field: selectedField,
                    candidate_fields: candidateFields,
                    existing_mappings:
                        existingMappings.length > 0
                            ? existingMappings
                            : undefined,
                });

                // Discard stale responses
                if (requestIdRef.current !== currentRequestId) return;

                const wires: SuggestedMappingWire[] = results.map((s) => {
                    const srcAttrId =
                        side === 'source' ? attrId : s.candidate_attribute_id;
                    const tgtAttrId =
                        side === 'source' ? s.candidate_attribute_id : attrId;
                    const srcPath =
                        side === 'source'
                            ? entityIdPath
                            : (s.candidate_entity_id_path ?? null);
                    const tgtPath =
                        side === 'source'
                            ? (s.candidate_entity_id_path ?? null)
                            : entityIdPath;
                    return {
                        id: `suggestion-${srcAttrId}-${tgtAttrId}`,
                        sourceAttrId: srcAttrId,
                        sourceEntityIdPath: srcPath,
                        targetAttrId: tgtAttrId,
                        targetEntityIdPath: tgtPath,
                        confidence: s.confidence,
                        reason: s.reason,
                    };
                });

                setSuggestedMappings(wires);
            } catch (err) {
                console.error('Failed to fetch mapping suggestions:', err);
                if (requestIdRef.current === currentRequestId) {
                    setSuggestedMappings([]);
                }
            } finally {
                if (requestIdRef.current === currentRequestId) {
                    setLoadingSuggestions(false);
                }
            }
        },
        [sourceModel, targetModel, transformations, groupId],
    );

    const confirmSuggestion = useCallback(
        (
            suggestionId: string,
        ): SuggestedMappingWire | null => {
            let found: SuggestedMappingWire | null = null;
            setSuggestedMappings((prev) => {
                const idx = prev.findIndex((s) => s.id === suggestionId);
                if (idx >= 0) {
                    found = prev[idx];
                    return prev.filter((_, i) => i !== idx);
                }
                return prev;
            });
            return found;
        },
        [],
    );

    const rejectSuggestion = useCallback((suggestionId: string) => {
        setSuggestedMappings((prev) =>
            prev.filter((s) => s.id !== suggestionId),
        );
    }, []);

    const clearSuggestions = useCallback(() => {
        setSuggestedMappings([]);
        setActiveFieldId(null);
    }, []);

    return {
        suggestedMappings,
        loadingSuggestions,
        activeFieldId,
        fetchSuggestions,
        confirmSuggestion,
        rejectSuggestion,
        clearSuggestions,
    };
}
