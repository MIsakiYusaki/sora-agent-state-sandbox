//! Windows notification transport. No dialogue body or private metadata in toasts.
#[cfg(target_os="windows")]
struct Apartment(bool);
#[cfg(target_os="windows")]
impl Apartment {
    fn initialize() -> std::result::Result<Self, ()> {
        use windows::Win32::System::WinRT::{RoInitialize, RO_INIT_MULTITHREADED};
        match unsafe { RoInitialize(RO_INIT_MULTITHREADED) } {
            Ok(()) => Ok(Self(true)),
            Err(e) if e.code().0 == 0x80010106u32 as i32 => Ok(Self(false)),
            Err(_) => Err(()),
        }
    }
}
#[cfg(target_os="windows")]
impl Drop for Apartment { fn drop(&mut self) { if self.0 { unsafe { windows::Win32::System::WinRT::RoUninitialize() }; } } }
#[cfg(target_os="windows")]
fn notifier() -> windows::core::Result<windows::UI::Notifications::ToastNotifier> {
    windows::UI::Notifications::ToastNotificationManager::CreateToastNotifierWithId(&windows::core::HSTRING::from("ai.chkong.sora"))
}
#[cfg(target_os="windows")]
fn submission_allowed(setting: windows::core::Result<windows::UI::Notifications::NotificationSetting>) -> bool {
    match setting {
        Ok(windows::UI::Notifications::NotificationSetting::Enabled) => true,
        Ok(_) => false,
        // Windows may create this app's notification record on the first Show.
        // Show still applies OS notification policy. Other query failures stay closed.
        Err(error) => error.code().0 == 0x80070490u32 as i32,
    }
}
pub fn permission() -> &'static str {
    #[cfg(target_os="windows")]
    {
        let Ok(_apartment) = Apartment::initialize() else { return "unavailable"; };
        return match notifier().and_then(|n| n.Setting()) {
            Ok(windows::UI::Notifications::NotificationSetting::Enabled) => "enabled",
            Ok(_) => "disabled", Err(_) => "unavailable",
        };
    }
    #[cfg(not(target_os="windows"))] { "unavailable" }
}
pub fn minute_of_day() -> u16 {
    #[cfg(target_os="windows")] { let time=unsafe { windows::Win32::System::SystemInformation::GetLocalTime() }; time.wHour*60+time.wMinute }
    #[cfg(not(target_os="windows"))] { 0 }
}
pub fn show() -> std::result::Result<(), ()> {
    #[cfg(target_os="windows")]
    {
        use windows::{core::HSTRING, Data::Xml::Dom::XmlDocument, UI::Notifications::ToastNotification};
        let _apartment=Apartment::initialize()?;
        let notifier=notifier().map_err(|_| ())?;
        if !submission_allowed(notifier.Setting()) { return Err(()); }
        let xml=XmlDocument::new().map_err(|_| ())?;
        xml.LoadXml(&HSTRING::from("<toast><visual><binding template='ToastGeneric'><text>Project Sora</text><text>Sora 已回应，打开客户端继续聊天。</text></binding></visual></toast>")).map_err(|_| ())?;
        let toast=ToastNotification::CreateToastNotification(&xml).map_err(|_| ())?;
        notifier.Show(&toast).map_err(|_| ())
    }
    #[cfg(not(target_os="windows"))] { Err(()) }
}

#[cfg(all(test,target_os="windows"))]
mod tests {
    use super::*;
    #[test]
    fn first_toast_can_register_without_overriding_disabled_or_other_errors() {
        use windows::{core::{Error, HRESULT}, UI::Notifications::NotificationSetting};
        assert!(submission_allowed(Ok(NotificationSetting::Enabled)));
        assert!(submission_allowed(Err(Error::from_hresult(HRESULT(0x80070490u32 as i32)))));
        for disabled in [NotificationSetting::DisabledForApplication, NotificationSetting::DisabledForUser,
            NotificationSetting::DisabledByGroupPolicy, NotificationSetting::DisabledByManifest] {
            assert!(!submission_allowed(Ok(disabled)));
        }
        assert!(!submission_allowed(Err(Error::from_hresult(HRESULT(0x80070005u32 as i32)))));
    }
    #[test]
    #[ignore = "Read-only local Windows notification environment diagnostic"]
    fn windows_notification_environment() {
        let apartment=unsafe { windows::Win32::System::WinRT::RoInitialize(windows::Win32::System::WinRT::RO_INIT_MULTITHREADED) };
        eprintln!("COM: {:?}", apartment.as_ref().map_err(|e|format!("0x{:08x}",e.code().0 as u32)));
        let status=notifier().and_then(|n|n.Setting());
        eprintln!("NotificationSetting: {:?}",status.map_err(|e|format!("0x{:08x}",e.code().0 as u32)));
        if apartment.is_ok() { unsafe { windows::Win32::System::WinRT::RoUninitialize() }; }
    }
}
