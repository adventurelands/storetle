/* tslint:disable */
/* eslint-disable */

/**
 * A parsed `.storetle` archive held in memory.
 */
export class WasmReader {
    free(): void;
    [Symbol.dispose](): void;
    /**
     * Decode document `index` to HTML. Decompresses only the containing
     * chunk (with a one-chunk cache, so sequential viewing is cheap).
     */
    get_html(index: number): string;
    /**
     * Parse an archive from bytes. `dict` is the external zstd dictionary
     * (`cube_dict_v10.bin`); pass `undefined`/`null` for files with an
     * embedded dictionary.
     */
    constructor(data: Uint8Array, dict?: Uint8Array | null);
    /**
     * Number of chunks.
     */
    readonly chunk_count: number;
    /**
     * Total number of documents.
     */
    readonly doc_count: number;
    /**
     * True if a dictionary (embedded or supplied) is loaded.
     */
    readonly has_dict: boolean;
    /**
     * True if the file carries an embedded dictionary.
     */
    readonly has_embedded_dict: boolean;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_wasmreader_free: (a: number, b: number) => void;
    readonly wasmreader_chunk_count: (a: number) => number;
    readonly wasmreader_doc_count: (a: number) => number;
    readonly wasmreader_get_html: (a: number, b: number) => [number, number, number, number];
    readonly wasmreader_has_dict: (a: number) => number;
    readonly wasmreader_has_embedded_dict: (a: number) => number;
    readonly wasmreader_new: (a: number, b: number, c: number, d: number) => [number, number, number];
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __externref_table_dealloc: (a: number) => void;
    readonly __wbindgen_free: (a: number, b: number, c: number) => void;
    readonly __wbindgen_malloc: (a: number, b: number) => number;
    readonly __wbindgen_start: () => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
