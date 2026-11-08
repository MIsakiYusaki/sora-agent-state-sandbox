//! Host-owned space catalog. No caller-provided filesystem paths or credentials.
use std::{env, fs::{self, File, OpenOptions}, io::Write, path::PathBuf};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

type Result<T> = std::result::Result<T, String>;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Space { pub id: String, pub name: String }
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Catalog {
    schema_version: String,
    active_id: String,
    spaces: Vec<Space>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pending_erasure: Option<PendingErasure>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    last_completed_erasure: Option<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PendingErasure {
    pub operation_id: String,
    pub profile_id: String,
    replacement: Space,
}
#[derive(Serialize)]
pub struct PrivacyState {
    pub schema_version: &'static str,
    pub profile_id: String,
    pub space_name: String,
    pub revision: String,
    pub pending_operation: Option<String>,
    pub last_completed_operation: Option<String>,
}
#[derive(Serialize)]
pub struct SpacePage { pub revision: String, pub active_id: String, pub spaces: Vec<Space> }
#[derive(Clone)]
pub struct ProfileStore { directory: PathBuf, state_root: PathBuf, subject: String, agent: String, initial: String }

#[derive(Clone, Serialize, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct NotificationPreferences { pub enabled: bool, pub quiet_enabled: bool, pub quiet_start: u16, pub quiet_end: u16 }
impl Default for NotificationPreferences { fn default() -> Self { Self { enabled:false, quiet_enabled:true, quiet_start:1320, quiet_end:480 } } }
impl NotificationPreferences {
    pub fn allows(&self, minute: u16) -> bool {
        self.enabled && (!self.quiet_enabled || if self.quiet_start == self.quiet_end { false }
            else if self.quiet_start < self.quiet_end { minute < self.quiet_start || minute >= self.quiet_end }
            else { minute >= self.quiet_end && minute < self.quiet_start })
    }
}
#[derive(Serialize)]
pub struct NotificationSettings { pub preferences: NotificationPreferences, pub revision: String }

pub fn config_root() -> Result<PathBuf> {
    let path = if let Some(root) = env::var_os("SORA_SANDBOX_CONFIG_DIR") { PathBuf::from(root) }
        else if let Some(root) = env::var_os("APPDATA") { PathBuf::from(root).join("ProjectSora").join("agent-state-sandbox") }
        else if let Some(root) = env::var_os("XDG_CONFIG_HOME") { PathBuf::from(root).join("project-sora").join("agent-state-sandbox") }
        else { PathBuf::from(env::var_os("HOME").ok_or("应用数据目录不可用")?).join(".config").join("project-sora").join("agent-state-sandbox") };
    if !path.is_absolute() { return Err("应用数据目录必须是绝对路径".into()); }
    Ok(path)
}
impl ProfileStore {
    pub fn from_environment(subject: &str, agent: &str, initial: &str) -> Result<Self> {
        let config = config_root()?;
        let state_root = env::var_os("SORA_GATEWAY_STATE_ROOT").or_else(|| env::var_os("SORA_SANDBOX_STATE_DIR")).map(PathBuf::from).unwrap_or_else(|| config.join("runtime"));
        if !state_root.is_absolute() { return Err("运行数据目录必须是绝对路径".into()); }
        let digest = format!("{:x}", Sha256::digest(format!("{subject}\x1f{agent}\x1f{initial}").as_bytes()));
        Ok(Self { directory: config.join("desktop-spaces").join(digest), state_root, subject: subject.into(), agent: agent.into(), initial: initial.into() })
    }
    fn file(&self) -> PathBuf { self.directory.join("catalog.json") }
    fn load(&self) -> Result<Catalog> {
        let value = match fs::read(self.file()) {
            Ok(bytes) => serde_json::from_slice::<Catalog>(&bytes).map_err(|_| "空间目录损坏，请保留现有文件并修复；不会自动覆盖")?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Catalog { schema_version: "DesktopSpaces.v1".into(), active_id: self.initial.clone(), spaces: vec![Space { id: self.initial.clone(), name: "我与 Sora".into() }], pending_erasure: None, last_completed_erasure: None },
            Err(_) => return Err("空间目录读取失败".into()),
        };
        if value.schema_version != "DesktopSpaces.v1" || value.spaces.len() > 100 || value.spaces.is_empty()
            || !value.spaces.iter().any(|s| s.id == value.active_id)
            || value.spaces.iter().any(|s| !valid_id(&s.id) || !valid_name(&s.name))
            || value.spaces.iter().enumerate().any(|(i,s)| value.spaces[..i].iter().any(|old| old.id == s.id)) {
            return Err("空间目录校验失败；不会自动覆盖".into());
        }
        if let Some(pending) = &value.pending_erasure {
            if pending.profile_id != value.active_id || !valid_id(&pending.operation_id)
                || !valid_id(&pending.replacement.id) || !valid_name(&pending.replacement.name)
                || value.spaces.iter().any(|space| space.id == pending.replacement.id) {
                return Err("删除恢复记录校验失败；空间保持关闭".into());
            }
        }
        Ok(value)
    }
    fn revision(catalog: &Catalog) -> String { format!("{:x}", Sha256::digest(serde_json::to_vec(catalog).expect("typed catalog"))) }
    pub fn list(&self) -> Result<SpacePage> { let c = self.load()?; Ok(SpacePage { revision: Self::revision(&c), active_id: c.active_id, spaces: c.spaces }) }
    pub fn active(&self) -> Result<String> { Ok(self.load()?.active_id) }
    pub fn pending_erasure(&self) -> Result<Option<PendingErasure>> { Ok(self.load()?.pending_erasure) }
    pub fn privacy_state(&self, id: &str) -> Result<PrivacyState> {
        let catalog = self.load()?;
        if catalog.active_id != id { return Err("空间身份已变化，请重新启动客户端".into()); }
        let space = catalog.spaces.iter().find(|space| space.id == id).ok_or("当前空间不存在")?;
        Ok(PrivacyState {
            schema_version: "DesktopPrivacyState.v1", profile_id: id.into(), space_name: space.name.clone(),
            revision: Self::revision(&catalog),
            pending_operation: catalog.pending_erasure.map(|pending| pending.operation_id),
            last_completed_operation: catalog.last_completed_erasure,
        })
    }
    pub fn begin_erasure(&self, id: &str, expected: &str) -> Result<PendingErasure> {
        let _lock = self.lock()?;
        let mut catalog = self.load()?;
        if catalog.active_id != id || Self::revision(&catalog) != expected { return Err("空间状态已变化，请重新读取后确认".into()); }
        if let Some(pending) = catalog.pending_erasure { return Ok(pending); }
        let pending = PendingErasure {
            operation_id: format!("erase_{}", Uuid::new_v4().simple()), profile_id: id.into(),
            replacement: Space { id: format!("space_{}", Uuid::new_v4().simple()), name: "我与 Sora".into() },
        };
        catalog.pending_erasure = Some(pending.clone());
        catalog.last_completed_erasure = None;
        self.write(&catalog)?;
        Ok(pending)
    }
    pub fn complete_erasure(&self, operation_id: &str) -> Result<()> {
        let _lock = self.lock()?;
        let mut catalog = self.load()?;
        if catalog.last_completed_erasure.as_deref() == Some(operation_id) { return Ok(()); }
        let pending = catalog.pending_erasure.as_ref().ok_or("没有可恢复的删除操作")?;
        if pending.operation_id != operation_id { return Err("删除回执与当前操作不一致".into()); }
        match fs::remove_file(self.preferences_file(&pending.profile_id)?) {
            Ok(()) => (),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err("通知偏好尚未清除，删除操作需要继续恢复".into()),
        }
        catalog.spaces.retain(|space| space.id != pending.profile_id);
        catalog.spaces.push(pending.replacement.clone());
        catalog.active_id = pending.replacement.id.clone();
        catalog.pending_erasure = None;
        catalog.last_completed_erasure = Some(operation_id.into());
        self.write(&catalog)
    }
    fn write(&self, c: &Catalog) -> Result<()> {
        self.write_json(&self.file(), c)
    }
    fn write_json<T: Serialize>(&self, destination: &std::path::Path, value: &T) -> Result<()> {
        let temporary = self.directory.join(format!("catalog-{}.tmp", Uuid::new_v4().simple()));
        let save = || -> Result<()> {
            let mut file = OpenOptions::new().create_new(true).write(true).open(&temporary).map_err(|_| "空间目录写入失败")?;
            file.write_all(&serde_json::to_vec(value).map_err(|_| "设置序列化失败")?).map_err(|_| "设置写入失败")?;
            file.sync_all().map_err(|_| "空间目录保存失败")?;
            drop(file);
            fs::rename(&temporary, destination).map_err(|_| "设置保存失败")?;
            Ok(())
        };
        let result = save();
        if result.is_err() { let _ = fs::remove_file(&temporary); }
        result
    }
    fn lock(&self) -> Result<File> {
        fs::create_dir_all(&self.directory).map_err(|_| "空间目录不可写")?;
        let file = OpenOptions::new().create(true).truncate(false).read(true).write(true).open(self.directory.join("catalog.lock")).map_err(|_| "空间目录不可写")?;
        file.try_lock().map_err(|_| "另一客户端正在修改空间目录，请稍后重新读取")?;
        Ok(file)
    }
    pub fn create(&self, name: &str, expected: &str) -> Result<SpacePage> {
        let name = name.trim(); if !valid_name(name) { return Err("空间名称需要1至60个字符，不能包含控制字符".into()); }
        let _lock = self.lock()?;
        let mut c = self.load()?;
        if c.pending_erasure.is_some() { return Err("请先在数据与隐私中完成删除恢复".into()); }
        if Self::revision(&c) != expected { return Err("空间目录已变化，请重新读取".into()); }
        if c.spaces.len() >= 100 { return Err("空间数量已达100，请先管理现有空间".into()); }
        c.spaces.push(Space { id: format!("space_{}", Uuid::new_v4().simple()), name: name.into() });
        self.write(&c)?; self.list()
    }
    pub fn validate_selection(&self, id: &str, expected: &str) -> Result<()> {
        let c = self.load()?;
        if c.pending_erasure.is_some() { return Err("请先在数据与隐私中完成删除恢复".into()); }
        if Self::revision(&c) != expected || !c.spaces.iter().any(|s| s.id == id) { return Err("空间目录已变化或目标不可用，请重新读取".into()); }
        Ok(())
    }
    pub fn select(&self, id: &str, expected: &str) -> Result<()> {
        let _lock = self.lock()?; self.validate_selection(id, expected)?;
        let mut c = self.load()?; c.active_id = id.into(); self.write(&c)
    }
    pub fn identity_directory(&self, id: &str) -> Result<PathBuf> {
        let c = self.load()?;
        if !c.spaces.iter().any(|s| s.id == id) { return Err("空间不存在".into()); }
        let digest = format!("{:x}", Sha256::digest(format!("{id}\x1f{}\x1f{}", self.subject, self.agent).as_bytes()));
        Ok(self.state_root.join("gateway-identities").join(digest))
    }
    fn preferences_file(&self, id: &str) -> Result<PathBuf> {
        self.identity_directory(id)?;
        Ok(self.directory.join(format!("notifications-{:x}.json", Sha256::digest(id.as_bytes()))))
    }
    pub fn notification_settings(&self, id: &str) -> Result<NotificationSettings> {
        if self.pending_erasure()?.is_some() { return Err("空间删除期间暂停通知".into()); }
        let preferences = match fs::read(self.preferences_file(id)?) {
            Ok(bytes) => serde_json::from_slice::<NotificationPreferences>(&bytes).map_err(|_| "通知设置损坏，请修复文件；不会覆盖")?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => NotificationPreferences::default(),
            Err(_) => return Err("通知设置无法读取".into()),
        };
        if preferences.quiet_start >= 1440 || preferences.quiet_end >= 1440 { return Err("安静时段超出有效范围".into()); }
        let revision = format!("{:x}", Sha256::digest(serde_json::to_vec(&preferences).expect("typed preferences")));
        Ok(NotificationSettings { preferences, revision })
    }
    pub fn save_notifications(&self, id: &str, preferences: NotificationPreferences, expected: &str) -> Result<NotificationSettings> {
        if preferences.quiet_start >= 1440 || preferences.quiet_end >= 1440 { return Err("安静时段超出有效范围".into()); }
        let _lock = self.lock()?;
        if self.pending_erasure()?.is_some() { return Err("删除恢复期间不能修改通知偏好".into()); }
        if self.notification_settings(id)?.revision != expected { return Err("通知设置已变化，请重新读取".into()); }
        self.write_json(&self.preferences_file(id)?, &preferences)?;
        self.notification_settings(id)
    }
}
fn valid_id(id: &str) -> bool { (3..=128).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b)) }
fn valid_name(name: &str) -> bool { !name.trim().is_empty() && name.chars().count() <= 60 && !name.chars().any(char::is_control) }

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    fn fixture(root: &Path) -> ProfileStore { ProfileStore { directory: root.join("catalog"), state_root: root.join("state"), subject:"fixture-user".into(), agent:"sora".into(), initial:"local".into() } }
    #[test]
    fn real_catalog_create_select_conflict_and_identity_isolation() {
        let root = env::temp_dir().join(format!("sora-space-test-{}", Uuid::new_v4().simple()));
        let store = fixture(&root); let original = store.list().unwrap();
        let created = store.create("独立测试空间", &original.revision).unwrap();
        assert_eq!(created.spaces.len(), 2); assert_eq!(created.active_id, "local");
        assert!(store.create("重复旧修订", &original.revision).is_err());
        let new_id = &created.spaces[1].id;
        assert_ne!(store.identity_directory("local").unwrap(), store.identity_directory(new_id).unwrap());
        assert!(store.identity_directory("../../outside").is_err());
        store.select(new_id, &created.revision).unwrap();
        assert_eq!(fixture(&root).active().unwrap(), *new_id);
        // A different process/store sees the same revision and selection.
        assert!(fixture(&root).select("local", &created.revision).is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn corrupted_catalog_is_preserved_and_concurrent_writes_fail_closed() {
        let root = env::temp_dir().join(format!("sora-space-test-{}", Uuid::new_v4().simple())); let store=fixture(&root);
        let initial = store.list().unwrap(); let locked = store.lock().unwrap();
        assert!(fixture(&root).create("并发", &initial.revision).is_err()); drop(locked);
        fs::write(store.file(), b"corrupted").unwrap();
        assert!(store.create("不能覆盖损坏文件", &initial.revision).is_err());
        assert_eq!(fs::read(store.file()).unwrap(), b"corrupted");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn notification_preferences_are_scoped_revision_checked_and_enforce_midnight() {
        let root=env::temp_dir().join(format!("sora-space-test-{}",Uuid::new_v4().simple())); let store=fixture(&root);
        let c=store.list().unwrap(); let c=store.create("另一个",&c.revision).unwrap();
        let p=store.notification_settings("local").unwrap(); assert!(!p.preferences.enabled);
        let p=store.save_notifications("local",NotificationPreferences {enabled:true,..NotificationPreferences::default()},&p.revision).unwrap();
        assert!(!p.preferences.allows(1320)); assert!(!p.preferences.allows(60)); assert!(p.preferences.allows(480)); assert!(p.preferences.allows(1319));
        assert!(store.save_notifications("local",p.preferences.clone(),"stale").is_err());
        assert!(!store.notification_settings(&c.spaces[1].id).unwrap().preferences.enabled);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn erasure_intent_survives_restart_and_replaces_only_the_erased_space() {
        let root = env::temp_dir().join(format!("sora-erasure-test-{}", Uuid::new_v4().simple()));
        let store = fixture(&root);
        let original = store.list().unwrap();
        let page = store.create("另一个空间", &original.revision).unwrap();
        let neighbour = page.spaces[1].id.clone();
        let settings = store.notification_settings("local").unwrap();
        store.save_notifications("local", NotificationPreferences { enabled: true, ..NotificationPreferences::default() }, &settings.revision).unwrap();
        let pending = store.begin_erasure("local", &page.revision).unwrap();
        let reopened = fixture(&root);
        let state = reopened.privacy_state("local").unwrap();
        assert_eq!(state.pending_operation.as_deref(), Some(pending.operation_id.as_str()));
        assert_eq!(reopened.begin_erasure("local", &state.revision).unwrap().operation_id, pending.operation_id);
        assert!(reopened.create("被阻止", &state.revision).is_err());
        assert!(reopened.select(&neighbour, &state.revision).is_err());
        assert!(reopened.save_notifications("local", settings.preferences, &settings.revision).is_err());
        assert!(reopened.complete_erasure("foreign-operation").is_err());
        reopened.complete_erasure(&pending.operation_id).unwrap();
        reopened.complete_erasure(&pending.operation_id).unwrap();
        let page = fixture(&root).list().unwrap();
        assert_ne!(page.active_id, "local");
        assert_eq!(page.spaces.len(), 2);
        assert!(page.spaces.iter().any(|space| space.id == neighbour));
        assert!(!page.spaces.iter().any(|space| space.id == "local"));
        let completed = reopened.privacy_state(&page.active_id).unwrap();
        assert_eq!(completed.last_completed_operation.as_deref(), Some(pending.operation_id.as_str()));
        assert!(completed.pending_operation.is_none());
        assert!(!fs::read_dir(&store.directory).unwrap().any(|item| item.unwrap().file_name().to_string_lossy().starts_with("notifications-")));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn preference_cleanup_failure_keeps_the_durable_erasure_intent() {
        let root = env::temp_dir().join(format!("sora-erasure-test-{}", Uuid::new_v4().simple()));
        let store = fixture(&root);
        let state = store.privacy_state("local").unwrap();
        let pending = store.begin_erasure("local", &state.revision).unwrap();
        let preferences = store.preferences_file("local").unwrap();
        fs::create_dir(&preferences).unwrap();
        assert!(store.complete_erasure(&pending.operation_id).is_err());
        assert!(fixture(&root).pending_erasure().unwrap().is_some());
        assert_eq!(fixture(&root).active().unwrap(), "local");
        fs::remove_dir(&preferences).unwrap();
        fixture(&root).complete_erasure(&pending.operation_id).unwrap();
        assert_ne!(fixture(&root).active().unwrap(), "local");
        fs::remove_dir_all(root).unwrap();
    }
}
