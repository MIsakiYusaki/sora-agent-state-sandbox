use zeroize::Zeroizing;

use crate::host::{HostError, HostResult};

#[cfg(target_os = "windows")]
pub fn prompt_api_key(provider_name: &str) -> HostResult<Zeroizing<String>> {
    use std::mem::size_of;
    use windows::{
        core::PCWSTR,
        Win32::{
            Foundation::{ERROR_CANCELLED, ERROR_SUCCESS},
            Security::Credentials::{
                CredUIPromptForCredentialsW, CREDUI_FLAGS_ALWAYS_SHOW_UI,
                CREDUI_FLAGS_DO_NOT_PERSIST, CREDUI_FLAGS_GENERIC_CREDENTIALS,
                CREDUI_FLAGS_KEEP_USERNAME, CREDUI_INFOW,
            },
        },
    };

    let wide = |value: &str| {
        value
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect::<Vec<_>>()
    };
    let target = wide(&format!("Project Sora/{provider_name}"));
    let caption = wide("连接模型");
    let message = wide(&format!(
        "请输入 {provider_name} API Key。密钥只会发送到本机安全凭据库。"
    ));
    let info = CREDUI_INFOW {
        cbSize: size_of::<CREDUI_INFOW>() as u32,
        pszMessageText: PCWSTR(message.as_ptr()),
        pszCaptionText: PCWSTR(caption.as_ptr()),
        ..Default::default()
    };
    let mut username = wide("API Key");
    username.resize(256, 0);
    let mut password = Zeroizing::new(vec![0u16; 4096]);
    let flags = CREDUI_FLAGS_GENERIC_CREDENTIALS
        | CREDUI_FLAGS_DO_NOT_PERSIST
        | CREDUI_FLAGS_ALWAYS_SHOW_UI
        | CREDUI_FLAGS_KEEP_USERNAME;
    let result = unsafe {
        CredUIPromptForCredentialsW(
            Some(&info as *const _),
            PCWSTR(target.as_ptr()),
            None,
            0,
            &mut username,
            password.as_mut_slice(),
            None,
            flags,
        )
    };
    if result == ERROR_CANCELLED {
        return Err(HostError::CredentialCancelled);
    }
    if result != ERROR_SUCCESS {
        return Err(HostError::CredentialPrompt(result.0));
    }
    let length = password
        .iter()
        .position(|unit| *unit == 0)
        .unwrap_or(password.len());
    let secret = String::from_utf16(&password[..length])
        .map_err(|_| HostError::CredentialPrompt(u32::MAX))?;
    if secret.trim().is_empty() {
        return Err(HostError::CredentialCancelled);
    }
    Ok(Zeroizing::new(secret))
}

#[cfg(not(target_os = "windows"))]
pub fn prompt_api_key(_provider_name: &str) -> HostResult<Zeroizing<String>> {
    Err(HostError::UnsupportedPlatform)
}
