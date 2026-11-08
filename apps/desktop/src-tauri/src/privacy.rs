use std::{fs::OpenOptions, io::Write, path::{Path, PathBuf}};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::host::{HostError, HostResult};

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportKind {
    MemorySummary,
    Diagnostic,
}

impl ExportKind {
    pub fn identifier(self) -> &'static str {
        match self {
            Self::MemorySummary => "memory_summary",
            Self::Diagnostic => "diagnostic",
        }
    }

    fn filename(self) -> &'static str {
        match self {
            Self::MemorySummary => "Project-Sora-memory-summary.json",
            Self::Diagnostic => "Project-Sora-diagnostic.json",
        }
    }
}

#[cfg(target_os = "windows")]
pub fn choose_destination(kind: ExportKind, owner: usize) -> HostResult<Option<PathBuf>> {
    use windows::{core::{PCWSTR, PWSTR}, Win32::{Foundation::HWND, UI::Controls::Dialogs::{
        CommDlgExtendedError, GetSaveFileNameW, OPENFILENAMEW,
        OFN_EXPLORER, OFN_NOCHANGEDIR, OFN_OVERWRITEPROMPT, OFN_PATHMUSTEXIST,
    }}};
    let mut filename = vec![0u16; 32768];
    for (target, source) in filename.iter_mut().zip(kind.filename().encode_utf16()) {
        *target = source;
    }
    let filter: Vec<u16> = "JSON 文档 (*.json)\0*.json\0\0".encode_utf16().collect();
    let title: Vec<u16> = "保存 Sora 导出文件\0".encode_utf16().collect();
    let extension: Vec<u16> = "json\0".encode_utf16().collect();
    let mut dialog = OPENFILENAMEW {
        lStructSize: std::mem::size_of::<OPENFILENAMEW>() as u32,
        hwndOwner: HWND(owner as *mut std::ffi::c_void),
        lpstrFilter: PCWSTR(filter.as_ptr()),
        nFilterIndex: 1,
        lpstrFile: PWSTR(filename.as_mut_ptr()),
        nMaxFile: filename.len() as u32,
        lpstrTitle: PCWSTR(title.as_ptr()),
        lpstrDefExt: PCWSTR(extension.as_ptr()),
        Flags: OFN_EXPLORER | OFN_NOCHANGEDIR | OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST,
        ..Default::default()
    };
    if !unsafe { GetSaveFileNameW(&mut dialog) }.as_bool() {
        return if unsafe { CommDlgExtendedError() }.0 == 0 {
            Ok(None)
        } else {
            Err(HostError::GatewayRejected("保存位置对话框未能打开，请重试。".into()))
        };
    }
    let length = filename.iter().position(|unit| *unit == 0).ok_or(HostError::InvalidResponse)?;
    let path = PathBuf::from(String::from_utf16(&filename[..length]).map_err(|_| HostError::InvalidResponse)?);
    if !path.is_absolute() || path.extension().and_then(|value| value.to_str()).is_none_or(|value| !value.eq_ignore_ascii_case("json")) {
        return Err(HostError::GatewayRejected("请选择 JSON 文件的完整保存位置。".into()));
    }
    Ok(Some(path))
}

#[cfg(not(target_os = "windows"))]
pub fn choose_destination(_kind: ExportKind, _owner: usize) -> HostResult<Option<PathBuf>> {
    Err(HostError::GatewayRejected("当前平台尚未支持原生文件保存。".into()))
}

pub fn save_export(destination: &Path, bytes: &[u8]) -> HostResult<()> {
    let parent = destination.parent().filter(|_| destination.is_absolute()).ok_or(HostError::InvalidResponse)?;
    let temporary = parent.join(format!(".sora-export-{}.tmp", Uuid::new_v4().simple()));
    let save = || -> std::io::Result<()> {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(&temporary, destination)
    };
    let result = save();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result.map_err(|_| HostError::GatewayRejected("导出文件未保存成功，请检查保存位置和可用空间后重试。".into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saves_complete_file_and_reports_missing_directory_without_false_success() {
        let directory = std::env::temp_dir().join(format!("sora-export-test-{}", Uuid::new_v4().simple()));
        std::fs::create_dir(&directory).unwrap();
        let file = directory.join("summary.json");
        save_export(&file, b"{\"fixture\":1}").unwrap();
        assert_eq!(std::fs::read(&file).unwrap(), b"{\"fixture\":1}");
        save_export(&file, b"{\"fixture\":2}").unwrap();
        assert_eq!(std::fs::read(&file).unwrap(), b"{\"fixture\":2}");
        assert!(save_export(&directory.join("missing").join("failure.json"), b"{}").is_err());
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 1);
        std::fs::remove_file(&file).unwrap();
        std::fs::remove_dir(&directory).unwrap();
    }
}
