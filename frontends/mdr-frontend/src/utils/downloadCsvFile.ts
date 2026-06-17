/** Serialize rows and download them as a CSV file */

export type CsvCell = string | number | boolean | null | undefined;

/** Escape a single CSV cell per RFC 4180 */
const escapeCsvCell = (cell: CsvCell): string => {
  if (cell == null) return '';
  const text = String(cell);
  if (/[",\r\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
};

/** Build a CSV string from a header row and data rows */
export const toCsv = (headers: string[], rows: CsvCell[][]): string => {
  const lines = [headers, ...rows].map((row) =>
    row.map(escapeCsvCell).join(',')
  );
  return lines.join('\r\n');
};

/** Download a CSV string as a file (UTF-8 BOM included so Excel detects encoding) */
export const downloadCsvFile = (csv: string, filename: string = 'data.csv'): void => {
  const safeFilename = sanitizeFilename(filename, 'data.csv');
  const BOM = '\uFEFF';
  const blob = new Blob([BOM, csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);

  try {
    const link = document.createElement('a');
    link.href = url;
    link.download = safeFilename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
};

/** Make a filename safe across major operating systems */
const sanitizeFilename = (input: string, fallback: string = 'data.csv'): string => {
  if (!input || typeof input !== 'string') return fallback;
  const MAX_FILENAME_LENGTH = 255;
  const WINDOWS_RESERVED_NAMES = new Set([
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
  ]);

  let name = input.trim();
  name = name.replace(/\s+/g, '_'); // Replace all whitespace runs with underscores
  name = name.replace(/[<>:"/\\|?*\x00-\x1F]/g, ''); // Remove path separators and invalid chars
  name = name.replace(/^[.\s]+|[.\s]+$/g, ''); // Remove leading/trailing dots and spaces
  name = name.replace(/_+/g, '_'); // Collapse repeated underscores
  if (!name) return fallback; // If empty after sanitizing, use fallback

  const ext = '.csv'; // Force .csv extension
  const lastDot = name.lastIndexOf('.');
  const maxBaseLength = MAX_FILENAME_LENGTH - ext.length;
  let base = lastDot > 0 ? name.slice(0, lastDot) : name;
  if (WINDOWS_RESERVED_NAMES.has(base.toUpperCase())) base = `_${base}`; // Prevent reserved Windows device names
  base = base.slice(0, maxBaseLength); // Enforce max length
  if (!base) base = 'data'; // Final fallback if base becomes empty

  return `${base}${ext}`;
};
