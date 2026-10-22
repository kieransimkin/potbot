//! Private local files, exclusive reservations and durable atomic replacement.
use crate::{Error, Result, MAX_JSON};
use std::{
    ffi::OsString,
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

/// Holds an exclusive OS lock until all public output for an operation is released.
pub struct Store {
    path: PathBuf,
    parent: PathBuf,
    _lock: File,
}
impl Store {
    pub fn open(path: &Path, create_parent: bool) -> Result<Self> {
        if !path.is_absolute() || path.file_name().is_none() {
            return Err(Error::Input);
        }
        let parent = path.parent().ok_or(Error::Input)?.to_path_buf();
        if !parent.exists() && create_parent {
            private_directory(&parent)?;
        }
        check_private(&parent, true)?;
        let mut lockname: OsString = path.file_name().unwrap().into();
        lockname.push(".lock");
        let lockpath = parent.join(lockname);
        let lock = match private_open(&lockpath, true) {
            Ok(file) => file,
            Err(_) => private_open(&lockpath, false)?,
        };
        lock_exclusive(&lock)?;
        Ok(Self {
            path: path.into(),
            parent,
            _lock: lock,
        })
    }
    pub fn read(&self) -> Result<Vec<u8>> {
        let mut file = private_open_read(&self.path)?;
        let mut bytes = Vec::new();
        Read::by_ref(&mut file)
            .take(MAX_JSON as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| Error::Storage)?;
        if bytes.is_empty() || bytes.len() > MAX_JSON {
            return Err(Error::Input);
        }
        Ok(bytes)
    }
    pub fn exists(&self) -> Result<bool> {
        match std::fs::symlink_metadata(&self.path) {
            Ok(_) => {
                check_private(&self.path, false)?;
                Ok(true)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(_) => Err(Error::Storage),
        }
    }
    /// The temporary file contains only authenticated ciphertext and public metadata.
    /// Both its data and the replacement directory entry are flushed before return.
    pub fn write(&self, bytes: &[u8], replace: bool) -> Result<()> {
        if bytes.is_empty() || bytes.len() > MAX_JSON || (!replace && self.exists()?) {
            return Err(Error::Storage);
        }
        if replace && !self.exists()? {
            return Err(Error::Storage);
        }
        let mut nonce = [0u8; 16];
        getrandom::getrandom(&mut nonce).map_err(|_| Error::Protection)?;
        let name = format!(".potbot-identity-{}.tmp", crate::hex(&nonce));
        let temporary = self.parent.join(name);
        let result = (|| {
            let mut file = private_open(&temporary, true)?;
            file.write_all(bytes).map_err(|_| Error::Storage)?;
            file.sync_all().map_err(|_| Error::Storage)?;
            drop(file);
            atomic_replace(&temporary, &self.path, replace)?;
            sync_directory(&self.parent)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&temporary);
        }
        result
    }
}

#[cfg(target_os = "linux")]
fn private_directory(path: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(path)
        .map_err(|_| Error::Storage)
}
#[cfg(target_os = "linux")]
fn check_private(path: &Path, directory: bool) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let m = std::fs::symlink_metadata(path).map_err(|_| Error::Storage)?;
    if m.file_type().is_symlink()
        || m.uid() != unsafe { libc::geteuid() }
        || m.mode() & 0o077 != 0
        || if directory {
            !m.is_dir()
        } else {
            !m.is_file() || m.nlink() != 1
        }
    {
        return Err(Error::Protection);
    }
    Ok(())
}
#[cfg(target_os = "linux")]
fn private_open(path: &Path, new: bool) -> Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(new)
        .mode(0o600)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| Error::Storage)?;
    check_private(path, false)?;
    // Check the opened object as well as its path before using its contents.
    use std::os::unix::fs::MetadataExt;
    let m = file.metadata().map_err(|_| Error::Storage)?;
    if !m.is_file()
        || m.uid() != unsafe { libc::geteuid() }
        || m.mode() & 0o077 != 0
        || m.nlink() != 1
    {
        return Err(Error::Protection);
    }
    Ok(file)
}
#[cfg(target_os = "linux")]
fn private_open_read(path: &Path) -> Result<File> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    check_private(path, false)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| Error::Storage)?;
    let m = file.metadata().map_err(|_| Error::Storage)?;
    if !m.is_file()
        || m.uid() != unsafe { libc::geteuid() }
        || m.mode() & 0o077 != 0
        || m.nlink() != 1
    {
        return Err(Error::Protection);
    }
    Ok(file)
}
#[cfg(target_os = "linux")]
fn lock_exclusive(file: &File) -> Result<()> {
    use std::os::fd::AsRawFd;
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        Err(Error::Storage)
    } else {
        Ok(())
    }
}
#[cfg(target_os = "linux")]
fn atomic_replace(from: &Path, to: &Path, replace: bool) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let source = std::ffi::CString::new(from.as_os_str().as_bytes()).map_err(|_| Error::Input)?;
    let destination =
        std::ffi::CString::new(to.as_os_str().as_bytes()).map_err(|_| Error::Input)?;
    let result = unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            source.as_ptr(),
            libc::AT_FDCWD,
            destination.as_ptr(),
            if replace { 0 } else { libc::RENAME_NOREPLACE },
        )
    };
    if result != 0 {
        Err(Error::Storage)
    } else {
        Ok(())
    }
}
#[cfg(target_os = "linux")]
fn sync_directory(path: &Path) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .and_then(|f| f.sync_all())
        .map_err(|_| Error::Storage)
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::{
        ffi::c_void,
        os::windows::{
            ffi::OsStrExt,
            fs::{MetadataExt, OpenOptionsExt},
            io::AsRawHandle,
        },
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::*,
        Security::{Authorization::*, *},
        Storage::FileSystem::*,
        System::{Threading::*, IO::OVERLAPPED},
    };
    fn wide(path: &Path) -> Vec<u16> {
        path.as_os_str().encode_wide().chain(Some(0)).collect()
    }
    struct Local(*mut c_void);
    impl Drop for Local {
        fn drop(&mut self) {
            unsafe {
                if !self.0.is_null() {
                    LocalFree(self.0);
                }
            }
        }
    }
    fn with_sid<T>(f: impl FnOnce(PSID) -> Result<T>) -> Result<T> {
        unsafe {
            let mut token = ptr::null_mut();
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return Err(Error::Protection);
            }
            let mut size = 0;
            GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut size);
            if size == 0 || size > 65536 {
                CloseHandle(token);
                return Err(Error::Protection);
            }
            // Word alignment is required for TOKEN_USER; Vec<u8> does not promise it.
            let mut buffer = vec![
                0usize;
                (size as usize + std::mem::size_of::<usize>() - 1)
                    / std::mem::size_of::<usize>()
            ];
            let result = GetTokenInformation(
                token,
                TokenUser,
                buffer.as_mut_ptr().cast(),
                size,
                &mut size,
            );
            CloseHandle(token);
            if result == 0 {
                return Err(Error::Protection);
            }
            f((*(buffer.as_ptr().cast::<TOKEN_USER>())).User.Sid)
        }
    }
    fn descriptor() -> Result<Local> {
        with_sid(|sid| unsafe {
            let mut string = ptr::null_mut();
            if ConvertSidToStringSidW(sid, &mut string) == 0 {
                return Err(Error::Protection);
            }
            let guard = Local(string.cast());
            let mut len = 0;
            while *string.add(len) != 0 {
                len += 1;
            }
            let sid_text = String::from_utf16(std::slice::from_raw_parts(string, len))
                .map_err(|_| Error::Protection)?;
            let sddl: Vec<u16> = format!("O:{sid_text}D:P(A;OICI;FA;;;{sid_text})")
                .encode_utf16()
                .chain(Some(0))
                .collect();
            let mut output = ptr::null_mut();
            let result = ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut output,
                ptr::null_mut(),
            );
            drop(guard);
            if result == 0 {
                Err(Error::Protection)
            } else {
                Ok(Local(output))
            }
        })
    }
    pub(super) fn private_directory(path: &Path) -> Result<()> {
        let sd = descriptor()?;
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.0,
            bInheritHandle: 0,
        };
        if unsafe { CreateDirectoryW(wide(path).as_ptr(), &attributes) } == 0 {
            return Err(Error::Storage);
        }
        check_private(path, true)
    }
    pub(super) fn check_private(path: &Path, directory: bool) -> Result<()> {
        let metadata = std::fs::symlink_metadata(path).map_err(|_| Error::Storage)?;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || if directory {
                !metadata.is_dir()
            } else {
                !metadata.is_file()
            }
        {
            return Err(Error::Protection);
        }
        with_sid(|sid| unsafe {
            let mut owner = ptr::null_mut();
            let mut acl = ptr::null_mut();
            let mut sd = ptr::null_mut();
            if GetNamedSecurityInfoW(
                wide(path).as_ptr(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                ptr::null_mut(),
                &mut acl,
                ptr::null_mut(),
                &mut sd,
            ) != ERROR_SUCCESS
            {
                return Err(Error::Protection);
            }
            let _guard = Local(sd);
            let mut control = 0;
            let mut revision = 0;
            if owner.is_null()
                || acl.is_null()
                || EqualSid(owner, sid) == 0
                || GetSecurityDescriptorControl(sd, &mut control, &mut revision) == 0
                || (*acl).AceCount == 0
            {
                return Err(Error::Protection);
            }
            // A directory must stop inherited broad ACLs. Files may inherit its
            // current-user-only ACL; every effective ACE is checked below.
            if directory && control & SE_DACL_PROTECTED == 0 {
                return Err(Error::Protection);
            }
            for index in 0..(*acl).AceCount as u32 {
                let mut ace = ptr::null_mut();
                if GetAce(acl, index, &mut ace) == 0 {
                    return Err(Error::Protection);
                }
                let allowed = &*(ace.cast::<ACCESS_ALLOWED_ACE>());
                if allowed.Header.AceType != 0
                    || EqualSid((&allowed.SidStart as *const u32).cast_mut().cast(), sid) == 0
                {
                    return Err(Error::Protection);
                }
            }
            Ok(())
        })
    }
    fn check_opened(file: &File) -> Result<()> {
        unsafe {
            let mut information: BY_HANDLE_FILE_INFORMATION = std::mem::zeroed();
            if GetFileInformationByHandle(file.as_raw_handle(), &mut information) == 0
                || information.dwFileAttributes
                    & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)
                    != 0
                || information.nNumberOfLinks != 1
            {
                return Err(Error::Protection);
            }
        }
        Ok(())
    }
    pub(super) fn private_open(path: &Path, new: bool) -> Result<File> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(new)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|_| Error::Storage)?;
        check_private(path, false)?;
        check_opened(&file)?;
        Ok(file)
    }
    pub(super) fn private_open_read(path: &Path) -> Result<File> {
        check_private(path, false)?;
        let file = OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|_| Error::Storage)?;
        check_private(path, false)?;
        check_opened(&file)?;
        Ok(file)
    }
    pub(super) fn lock_exclusive(file: &File) -> Result<()> {
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        if unsafe {
            LockFileEx(
                file.as_raw_handle(),
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                u32::MAX,
                u32::MAX,
                &mut overlapped,
            )
        } == 0
        {
            Err(Error::Storage)
        } else {
            Ok(())
        }
    }
    pub(super) fn atomic_replace(from: &Path, to: &Path, replace: bool) -> Result<()> {
        if unsafe {
            MoveFileExW(
                wide(from).as_ptr(),
                wide(to).as_ptr(),
                MOVEFILE_WRITE_THROUGH
                    | if replace {
                        MOVEFILE_REPLACE_EXISTING
                    } else {
                        0
                    },
            )
        } == 0
        {
            Err(Error::Storage)
        } else {
            Ok(())
        }
    }
    pub(super) fn sync_directory(_path: &Path) -> Result<()> {
        Ok(())
    } // MoveFileExW WRITE_THROUGH above.
}
#[cfg(windows)]
use windows::*;

#[cfg(not(any(target_os = "linux", windows)))]
fn private_directory(_path: &Path) -> Result<()> {
    Err(Error::Unsupported)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn check_private(_path: &Path, _directory: bool) -> Result<()> {
    Err(Error::Unsupported)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn private_open(_path: &Path, _new: bool) -> Result<File> {
    Err(Error::Unsupported)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn private_open_read(_path: &Path) -> Result<File> {
    Err(Error::Unsupported)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn lock_exclusive(_file: &File) -> Result<()> {
    Err(Error::Unsupported)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn atomic_replace(_from: &Path, _to: &Path, _replace: bool) -> Result<()> {
    Err(Error::Unsupported)
}
#[cfg(not(any(target_os = "linux", windows)))]
fn sync_directory(_path: &Path) -> Result<()> {
    Err(Error::Unsupported)
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};
    #[test]
    fn atomic_private_files_lock_and_no_overwrite() {
        let temp = tempfile::tempdir().unwrap();
        let directory = temp.path().join("private");
        let path = directory.join("identity.json");
        let store = Store::open(&path, true).unwrap();
        assert!(!store.exists().unwrap());
        assert!(Store::open(&path, false).is_err());
        store.write(b"encrypted first snapshot", false).unwrap();
        assert!(store.write(b"replace forbidden", false).is_err());
        store.write(b"encrypted next snapshot", true).unwrap();
        assert_eq!(store.read().unwrap(), b"encrypted next snapshot");
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        drop(store);
        Store::open(&path, false).unwrap();
    }
    #[test]
    fn rejects_public_permissions_and_symlinks() {
        let temp = tempfile::tempdir().unwrap();
        let directory = temp.path().join("private");
        let path = directory.join("identity.json");
        let store = Store::open(&path, true).unwrap();
        store.write(b"encrypted", false).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(store.read().is_err());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        drop(store);
        let alias = directory.join("alias.json");
        symlink(&path, &alias).unwrap();
        let alias_store = Store::open(&alias, false).unwrap();
        assert!(alias_store.read().is_err());
    }
}
