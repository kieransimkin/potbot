//! Process-local native memory boundary. No production unprotected fallback.
use std::alloc::{GlobalAlloc, Layout};
use std::ptr;

pub struct LockedAllocator;

#[repr(C)]
struct Header {
    allocation: *mut u8,
    total: usize,
    usable: usize,
    page: usize,
}

// Every allocation is independently guarded and locked, including allocations made
// inside the chosen cryptographic providers. Deallocation wipes the entire usable
// region, not just the caller's logical length. No allocator diagnostics use heap.
unsafe impl GlobalAlloc for LockedAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let page = 4096usize;
        #[cfg(target_os = "linux")]
        if libc::sysconf(libc::_SC_PAGESIZE) != page as libc::c_long {
            return ptr::null_mut();
        }
        let Some(need) = layout
            .size()
            .max(1)
            .checked_add(layout.align())
            .and_then(|v| v.checked_add(std::mem::size_of::<Header>()))
        else {
            return ptr::null_mut();
        };
        let Some(usable) = need.checked_add(page - 1).map(|v| v / page * page) else {
            return ptr::null_mut();
        };
        let Some(total) = usable.checked_add(2 * page) else {
            return ptr::null_mut();
        };
        let base = os_allocate(total, usable, page);
        if base.is_null() {
            return ptr::null_mut();
        }
        let first = base.add(page).add(std::mem::size_of::<Header>()) as usize;
        let address = (first + layout.align() - 1) & !(layout.align() - 1);
        let result = address as *mut u8;
        ptr::write_unaligned(
            result.sub(std::mem::size_of::<Header>()).cast::<Header>(),
            Header {
                allocation: base,
                total,
                usable,
                page,
            },
        );
        result
    }
    unsafe fn dealloc(&self, address: *mut u8, _layout: Layout) {
        if address.is_null() {
            return;
        }
        let header =
            ptr::read_unaligned(address.sub(std::mem::size_of::<Header>()).cast::<Header>());
        let data = header.allocation.add(header.page);
        for i in 0..header.usable {
            ptr::write_volatile(data.add(i), 0);
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
        os_release(header.allocation, header.total, header.usable, header.page);
    }
}

#[cfg(target_os = "linux")]
unsafe fn os_allocate(total: usize, usable: usize, page: usize) -> *mut u8 {
    let base = libc::mmap(
        ptr::null_mut(),
        total,
        libc::PROT_NONE,
        libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
        -1,
        0,
    );
    if base == libc::MAP_FAILED {
        return ptr::null_mut();
    }
    let data = base.cast::<u8>().add(page).cast();
    if libc::mprotect(data, usable, libc::PROT_READ | libc::PROT_WRITE) != 0
        || libc::mlock(data, usable) != 0
        || libc::madvise(data, usable, libc::MADV_DONTDUMP) != 0
    {
        libc::munmap(base, total);
        let message = b"potbot identity: protected memory unavailable; require at least 96 MiB locked memory; no fallback\n";
        libc::write(2, message.as_ptr().cast(), message.len());
        return ptr::null_mut();
    }
    base.cast()
}

#[cfg(target_os = "linux")]
unsafe fn os_release(base: *mut u8, total: usize, usable: usize, page: usize) {
    libc::munlock(base.add(page).cast(), usable);
    libc::munmap(base.cast(), total);
}

#[cfg(windows)]
unsafe fn os_allocate(total: usize, usable: usize, page: usize) -> *mut u8 {
    use windows_sys::Win32::System::Memory::*;
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, SetProcessWorkingSetSize};
    let base = VirtualAlloc(ptr::null(), total, MEM_RESERVE | MEM_COMMIT, PAGE_NOACCESS);
    if base.is_null() {
        return ptr::null_mut();
    }
    let data = base.cast::<u8>().add(page).cast();
    let mut old = 0;
    if VirtualProtect(data, usable, PAGE_READWRITE, &mut old) == 0 {
        VirtualFree(base, 0, MEM_RELEASE);
        return ptr::null_mut();
    }
    if VirtualLock(data, usable) == 0 {
        // This only changes this broker process's working-set limits. It neither
        // obtains a privilege nor turns failure into an unprotected allocation.
        SetProcessWorkingSetSize(GetCurrentProcess(), 128 * 1024 * 1024, 256 * 1024 * 1024);
        if VirtualLock(data, usable) == 0 {
            VirtualFree(base, 0, MEM_RELEASE);
            return ptr::null_mut();
        }
    }
    base.cast()
}

#[cfg(windows)]
unsafe fn os_release(base: *mut u8, _total: usize, usable: usize, page: usize) {
    use windows_sys::Win32::System::Memory::*;
    VirtualUnlock(base.add(page).cast(), usable);
    VirtualFree(base.cast(), 0, MEM_RELEASE);
}

#[cfg(not(any(target_os = "linux", windows)))]
unsafe fn os_allocate(_total: usize, _usable: usize, _page: usize) -> *mut u8 {
    ptr::null_mut()
}
#[cfg(not(any(target_os = "linux", windows)))]
unsafe fn os_release(_base: *mut u8, _total: usize, _usable: usize, _page: usize) {}

/// All sensitive work is performed on a separate fixed-size native stack, so the
/// memory containing provider temporaries can be locked before the first secret.
pub fn run_protected(work: fn() -> i32) -> i32 {
    #[cfg(target_os = "linux")]
    {
        let joined = std::thread::Builder::new()
            .name("identity-custody".into())
            .stack_size(1024 * 1024)
            .spawn(move || unsafe {
                let limits = libc::rlimit {
                    rlim_cur: 0,
                    rlim_max: 0,
                };
                if libc::setrlimit(libc::RLIMIT_CORE, &limits) != 0
                    || libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) != 0
                {
                    eprintln!("identity protection unavailable: process dump protection");
                    return 78;
                }
                let mut attrs: libc::pthread_attr_t = std::mem::zeroed();
                let mut stack = ptr::null_mut();
                let mut size = 0;
                if libc::pthread_getattr_np(libc::pthread_self(), &mut attrs) != 0 {
                    return 78;
                }
                let result = libc::pthread_attr_getstack(&attrs, &mut stack, &mut size);
                libc::pthread_attr_destroy(&mut attrs);
                if result != 0
                    || libc::mlock(stack, size) != 0
                    || libc::madvise(stack, size, libc::MADV_DONTDUMP) != 0
                {
                    eprintln!("identity protection unavailable: native operation stack");
                    return 78;
                }
                work()
            });
        match joined {
            Ok(thread) => thread.join().unwrap_or(70),
            Err(_) => 78,
        }
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
        use windows_sys::Win32::System::Threading::*;
        unsafe extern "system" fn entry(arg: *mut std::ffi::c_void) -> u32 {
            use windows_sys::Win32::System::Memory::*;
            let f: fn() -> i32 = std::mem::transmute(arg);
            let mut low = 0usize;
            let mut high = 0usize;
            GetCurrentThreadStackLimits(&mut low, &mut high);
            // Inspect the complete stack reservation. Any automatic growth
            // guard becomes a fixed no-access boundary: stack growth must fail,
            // never expose a new unlocked page to a provider temporary.
            let mut reservation: MEMORY_BASIC_INFORMATION = std::mem::zeroed();
            if VirtualQuery(
                (&low as *const usize).cast(),
                &mut reservation,
                std::mem::size_of_val(&reservation),
            ) == 0
            {
                return 78;
            }
            let mut cursor = reservation.AllocationBase as usize;
            if cursor == 0 || cursor > low || high <= low {
                return 78;
            }
            let mut fixed_boundary = false;
            while cursor < high {
                let mut info: MEMORY_BASIC_INFORMATION = std::mem::zeroed();
                if VirtualQuery(cursor as *const _, &mut info, std::mem::size_of_val(&info)) == 0
                    || info.RegionSize == 0
                {
                    return 78;
                }
                if info.State == MEM_COMMIT && info.Protect & PAGE_GUARD != 0 {
                    let mut previous = 0;
                    if VirtualProtect(
                        info.BaseAddress,
                        info.RegionSize,
                        PAGE_NOACCESS,
                        &mut previous,
                    ) == 0
                    {
                        return 78;
                    }
                    fixed_boundary = true;
                } else if info.State == MEM_COMMIT && info.Protect & PAGE_NOACCESS == 0 {
                    if VirtualLock(info.BaseAddress, info.RegionSize) == 0 {
                        SetProcessWorkingSetSize(
                            GetCurrentProcess(),
                            128 * 1024 * 1024,
                            256 * 1024 * 1024,
                        );
                        if VirtualLock(info.BaseAddress, info.RegionSize) == 0 {
                            return 78;
                        }
                    }
                } else if info.Protect & PAGE_NOACCESS != 0 {
                    fixed_boundary = true;
                }
                cursor = info.BaseAddress as usize + info.RegionSize;
            }
            if !fixed_boundary {
                return 78;
            }
            f() as u32
        }
        unsafe {
            // No STACK_SIZE_PARAM_IS_A_RESERVATION: request a committed 1 MiB
            // operation stack, then lock its committed pages in entry().
            let thread = CreateThread(
                ptr::null(),
                1024 * 1024,
                Some(entry),
                work as *mut _,
                0,
                ptr::null_mut(),
            );
            if thread.is_null() {
                return 78;
            }
            let wait = WaitForSingleObject(thread, INFINITE);
            let mut result = 78;
            if wait == WAIT_OBJECT_0 {
                GetExitCodeThread(thread, &mut result);
            }
            CloseHandle(thread);
            result as i32
        }
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = work;
        78
    }
}

/// A bounded check before reading any secrets. This is a host capability check,
/// not an assertion that a hardware device or a complete threat model is certified.
pub fn preflight() -> serde_json::Value {
    #[cfg(target_os = "linux")]
    let (platform, limit, reason) = unsafe {
        let mut locked: libc::rlimit = std::mem::zeroed();
        if libc::getrlimit(libc::RLIMIT_MEMLOCK, &mut locked) != 0 {
            ("linux", None, "protected-memory-unavailable")
        } else if locked.rlim_cur < 96 * 1024 * 1024 {
            ("linux", Some(locked.rlim_cur), "locked-memory-limit")
        } else {
            ("linux", Some(locked.rlim_cur), memory_probe())
        }
    };
    #[cfg(windows)]
    let (platform, limit, reason) = ("windows", None::<u64>, unsafe { memory_probe() });
    #[cfg(not(any(target_os = "linux", windows)))]
    let (platform, limit, reason) = ("unsupported", None::<u64>, "unsupported-platform");
    serde_json::json!({"schema_version":1,"protection_profile":crate::PROTECTION_PROFILE,
        "platform":platform,"ready":reason=="ready","required_locked_memory_bytes":96*1024*1024,
        "locked_memory_limit_bytes":limit,"reason":reason})
}
#[cfg(any(target_os = "linux", windows))]
unsafe fn memory_probe() -> &'static str {
    let region = os_allocate(64 * 1024 * 1024 + 8192, 64 * 1024 * 1024, 4096);
    if region.is_null() {
        "protected-memory-unavailable"
    } else {
        os_release(region, 64 * 1024 * 1024 + 8192, 64 * 1024 * 1024, 4096);
        "ready"
    }
}
