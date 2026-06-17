import api from "./api";

const apiBaseUrl = import.meta.env.VITE_API_URL;

/**
 * A single education standard exposed by the EDUcore server (e.g. LIF, PESC,
 * CASE, CEDS, Ed-Fi, CTDL). These are browsable standards that can be imported
 * into the MDR as real DataModels.
 */
export interface EducoreStandard {
  key: string;
  title: string;
  /** Short, recognizable label (e.g. "CASE", "LIF") for display. */
  displayName?: string | null;
  /** Whether this standard currently has a loader and can be imported. */
  importable?: boolean;
  version: string | null;
  description: string | null;
}

/**
 * Result of importing an EDUcore standard. The standard is converted into a
 * real MDR DataModel; the returned `Id` can be loaded via the existing
 * `/datamodels/with_details/{Id}` flow.
 */
export interface EducoreImportResult {
  Id: number;
  Name: string;
  alreadyExisted: boolean;
}

/**
 * List the education standards available from the EDUcore server.
 */
export const listEducoreStandards = async (): Promise<EducoreStandard[]> => {
  try {
    const result = await api.get<EducoreStandard[]>(
      `${apiBaseUrl}/educore/standards`
    );
    return result.data ?? [];
  } catch (error) {
    console.error("Error fetching EDUcore standards:", error);
    throw error;
  }
};

/**
 * Import an EDUcore standard, converting it into a real MDR DataModel.
 * Returns the resulting DataModel Id (and whether it already existed).
 */
export const importEducoreStandard = async (
  key: string
): Promise<EducoreImportResult> => {
  try {
    const result = await api.post<EducoreImportResult>(
      `${apiBaseUrl}/educore/standards/${encodeURIComponent(key)}/import`
    );
    return result.data;
  } catch (error) {
    console.error("Error importing EDUcore standard:", error);
    throw error;
  }
};

/**
 * Result of materializing the canonical EDUcore MAPS_TO crosswalk between two
 * imported specs into a real transformation group.
 */
export interface EducoreLoadMappingsResult {
  transformationGroupId: number;
  groupName: string;
  transformationsCreated: number;
  sourcePairsMapped: number;
  edgesFound: number;
  edgesUnresolved: number;
  alreadyExisted: boolean;
  /** True when no edges exist this direction but the reverse direction has data. */
  reverseDirectionHint: boolean;
}

/**
 * A directed pair of imported EDUcore specs that has a canonical (MAPS_TO)
 * crosswalk available to load.
 */
export interface EducoreMappingPair {
  sourceShort: string;
  targetShort: string;
  sourceDataModelId: number;
  targetDataModelId: number;
  /** Field-level MAPS_TO edges in the graph for this directed pair. */
  mappingCount: number;
}

/**
 * List the directed pairs (both specs imported) that have a canonical crosswalk
 * ready to load.
 */
export const listEducoreMappingPairs = async (): Promise<
  EducoreMappingPair[]
> => {
  try {
    const result = await api.get<EducoreMappingPair[]>(
      `${apiBaseUrl}/educore/mappings/available`
    );
    return result.data ?? [];
  } catch (error) {
    console.error("Error fetching EDUcore mapping pairs:", error);
    throw error;
  }
};

/**
 * Load the authoritative (MAPS_TO) cross-standard mappings between two
 * EDUcore-imported DataModels into a transformation group. Returns the group Id
 * to navigate to so the existing mappings UI renders the wired transformations.
 */
export const loadEducoreMappings = async (
  sourceDataModelId: number,
  targetDataModelId: number
): Promise<EducoreLoadMappingsResult> => {
  try {
    const result = await api.post<EducoreLoadMappingsResult>(
      `${apiBaseUrl}/educore/mappings/load`,
      { sourceDataModelId, targetDataModelId }
    );
    return result.data;
  } catch (error) {
    console.error("Error loading EDUcore mappings:", error);
    throw error;
  }
};
