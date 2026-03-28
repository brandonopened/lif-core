import api from './api';

export interface FieldInfo {
    attribute_id: number;
    attribute_name: string;
    entity_name: string;
    entity_id_path?: string | null;
    description?: string | null;
    data_type?: string | null;
    value_set_values?: string[] | null;
}

export interface ExistingMapping {
    source_name: string;
    target_name: string;
}

export interface SuggestedMapping {
    candidate_attribute_id: number;
    candidate_entity_id_path?: string | null;
    confidence: number;
    reason: string;
}

export interface SuggestMappingsRequest {
    selected_side: 'source' | 'target';
    selected_field: FieldInfo;
    candidate_fields: FieldInfo[];
    existing_mappings?: ExistingMapping[];
}

export const suggestMappings = async (
    request: SuggestMappingsRequest
): Promise<SuggestedMapping[]> => {
    const res = await api.post('/suggest_mappings/', request);
    return res.data?.suggestions ?? [];
};
