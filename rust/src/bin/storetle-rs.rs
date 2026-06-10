//! storetle-rs — CLI reader for `.storetle` archives.
//!
//! Subcommands:
//!   ls <file>              doc count + per-chunk stats
//!   get <file> <index>     print one document's HTML to stdout
//!   unpack <file> <outdir> write every document as doc_NNNNNN.html
//!
//! Dictionary resolution (for files without an embedded dictionary):
//!   --dict <path>, else $STORETLE_DICT, else cube_dict_v10.bin next to the
//!   input file, else cube_dict_v10.bin in the current directory.

use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use storetle::StoretleReader;

const USAGE: &str = "\
storetle-rs — reader for .storetle archives

USAGE:
    storetle-rs ls     <file> [--dict <path>]
    storetle-rs get    <file> <index> [--dict <path>]
    storetle-rs unpack <file> <outdir> [--dict <path>]

OPTIONS:
    --dict <path>   zstd dictionary (cube_dict_v10.bin). If omitted, tries
                    $STORETLE_DICT, then cube_dict_v10.bin next to <file>,
                    then ./cube_dict_v10.bin. Unneeded for files with an
                    embedded dictionary.
    -h, --help      show this help
";

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("error: {msg}");
            ExitCode::FAILURE
        }
    }
}

struct Args {
    cmd: String,
    positional: Vec<String>,
    dict: Option<PathBuf>,
}

fn parse_args() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let mut cmd = None;
    let mut positional = Vec::new();
    let mut dict = None;
    while let Some(a) = it.next() {
        match a.as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            "--dict" => {
                let p = it.next().ok_or("--dict requires a path")?;
                dict = Some(PathBuf::from(p));
            }
            _ => {
                if cmd.is_none() {
                    cmd = Some(a);
                } else {
                    positional.push(a);
                }
            }
        }
    }
    Ok(Args {
        cmd: cmd.ok_or_else(|| format!("no subcommand given\n\n{USAGE}"))?,
        positional,
        dict,
    })
}

/// Find dictionary bytes per the resolution order in the help text.
fn resolve_dict(explicit: Option<&Path>, input: &Path) -> Result<Option<Vec<u8>>, String> {
    if let Some(p) = explicit {
        return std::fs::read(p)
            .map(Some)
            .map_err(|e| format!("cannot read dictionary {}: {e}", p.display()));
    }
    if let Ok(p) = std::env::var("STORETLE_DICT") {
        let p = PathBuf::from(p);
        if p.exists() {
            return std::fs::read(&p)
                .map(Some)
                .map_err(|e| format!("cannot read dictionary {}: {e}", p.display()));
        }
    }
    let mut candidates = Vec::new();
    if let Some(dir) = input.parent() {
        candidates.push(dir.join("cube_dict_v10.bin"));
    }
    candidates.push(PathBuf::from("cube_dict_v10.bin"));
    for c in candidates {
        if c.exists() {
            return std::fs::read(&c)
                .map(Some)
                .map_err(|e| format!("cannot read dictionary {}: {e}", c.display()));
        }
    }
    Ok(None)
}

fn open_reader(
    file: &str,
    dict_flag: Option<&Path>,
) -> Result<StoretleReader<std::io::BufReader<std::fs::File>>, String> {
    let input = Path::new(file);
    let dict = resolve_dict(dict_flag, input)?;
    StoretleReader::open(input, dict).map_err(|e| format!("{file}: {e}"))
}

fn run() -> Result<(), String> {
    let args = parse_args()?;
    match args.cmd.as_str() {
        "ls" => {
            let [file] = args.positional.as_slice() else {
                return Err(format!("usage: storetle-rs ls <file>\n\n{USAGE}"));
            };
            let r = open_reader(file, args.dict.as_deref())?;
            let file_size = std::fs::metadata(file).map_err(|e| e.to_string())?.len();
            println!("file:           {file}");
            println!("file size:      {file_size} bytes");
            println!("documents:      {}", r.doc_count());
            println!("chunks:         {}", r.chunk_count());
            println!(
                "dictionary:     {}",
                if r.has_embedded_dict() {
                    "embedded"
                } else if r.has_dict() {
                    "external (loaded)"
                } else {
                    "none loaded"
                }
            );
            println!();
            println!(
                "{:>6}  {:>12}  {:>8}  {:>12}",
                "chunk", "offset", "docs", "encoded_b"
            );
            for (i, c) in r.chunks().iter().enumerate() {
                println!(
                    "{:>6}  {:>12}  {:>8}  {:>12}",
                    i, c.file_offset, c.doc_count, c.orig_total
                );
            }
            Ok(())
        }
        "get" => {
            let [file, index] = args.positional.as_slice() else {
                return Err(format!("usage: storetle-rs get <file> <index>\n\n{USAGE}"));
            };
            let idx: u64 = index
                .parse()
                .map_err(|_| format!("invalid index: {index}"))?;
            let mut r = open_reader(file, args.dict.as_deref())?;
            let html = r.get(idx).map_err(|e| e.to_string())?;
            let mut stdout = std::io::stdout().lock();
            stdout
                .write_all(html.as_bytes())
                .and_then(|_| stdout.write_all(b"\n"))
                .map_err(|e| e.to_string())
        }
        "unpack" => {
            let [file, outdir] = args.positional.as_slice() else {
                return Err(format!(
                    "usage: storetle-rs unpack <file> <outdir>\n\n{USAGE}"
                ));
            };
            let outdir = Path::new(outdir);
            std::fs::create_dir_all(outdir).map_err(|e| e.to_string())?;
            let mut r = open_reader(file, args.dict.as_deref())?;
            let total = r.doc_count();
            let mut n: u64 = 0;
            for doc in r.iter_docs() {
                let html = doc.map_err(|e| format!("doc {n}: {e}"))?;
                let path = outdir.join(format!("doc_{n:06}.html"));
                std::fs::write(&path, html.as_bytes())
                    .map_err(|e| format!("{}: {e}", path.display()))?;
                n += 1;
            }
            eprintln!("unpacked {n}/{total} documents into {}", outdir.display());
            Ok(())
        }
        other => Err(format!("unknown subcommand: {other}\n\n{USAGE}")),
    }
}
