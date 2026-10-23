//! Public JSON is the only stdout protocol. Passwords and recovery words never
//! cross the Python/native boundary. There is deliberately no raw-key command.
use potbot_identity::provisioning::{AgentSignRequest, AgentVault, ProvisionRequest};
use potbot_identity::{
    canonical, entropy_from_words, epoch, files::Store, now, parse, secure, ClockGuard, Error,
    Policy, Result, SignRequest, Vault,
};
use serde::Serialize;
use serde_json::json;
use std::{
    collections::BTreeMap,
    fs::File,
    io::{Read, Write},
    path::{Path, PathBuf},
};
use zeroize::Zeroizing;

#[global_allocator]
static PROTECTED_HEAP: secure::LockedAllocator = secure::LockedAllocator;

fn main() {
    // Prevent a panic formatter from displaying a provider value or request.
    std::panic::set_hook(Box::new(|_| {
        eprintln!("potbot identity: native operation failed")
    }));
    std::process::exit(secure::run_protected(entry));
}
fn entry() -> i32 {
    match execute() {
        Ok(code) => code,
        Err(error) => {
            eprintln!("potbot identity: {error}");
            match error {
                Error::Protection | Error::Unsupported => 78,
                Error::Input | Error::Policy => 64,
                Error::Authentication | Error::Identity => 77,
                _ => 74,
            }
        }
    }
}
struct Args {
    command: String,
    options: BTreeMap<String, String>,
}
impl Args {
    fn parse() -> Result<Self> {
        let mut args = std::env::args_os().skip(1);
        let command = args
            .next()
            .ok_or(Error::Input)?
            .into_string()
            .map_err(|_| Error::Input)?;
        let mut options = BTreeMap::new();
        while let Some(key) = args.next() {
            let key = key.into_string().map_err(|_| Error::Input)?;
            if !key.starts_with("--") || options.contains_key(&key) {
                return Err(Error::Input);
            }
            let value = args
                .next()
                .ok_or(Error::Input)?
                .into_string()
                .map_err(|_| Error::Input)?;
            if value.len() > 4096 {
                return Err(Error::Input);
            }
            options.insert(key, value);
        }
        Ok(Self { command, options })
    }
    fn allowed(&self, names: &[&str]) -> Result<()> {
        if self.options.keys().any(|k| !names.contains(&k.as_str())) {
            Err(Error::Input)
        } else {
            Ok(())
        }
    }
    fn get(&self, name: &str) -> Result<&str> {
        self.options
            .get(name)
            .map(String::as_str)
            .ok_or(Error::Input)
    }
    fn path(&self, name: &str) -> Result<PathBuf> {
        let path = PathBuf::from(self.get(name)?);
        if !path.is_absolute() {
            Err(Error::Input)
        } else {
            Ok(path)
        }
    }
    fn expected(&self) -> Result<&str> {
        let hash = self.get("--expect-signer")?;
        if hash.len() != 64
            || !hash
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            Err(Error::Input)
        } else {
            Ok(hash)
        }
    }
    fn number(&self, name: &str) -> Result<u32> {
        let raw = self.get(name)?;
        let number = raw.parse::<u32>().map_err(|_| Error::Input)?;
        if raw != number.to_string() {
            return Err(Error::Input);
        }
        Ok(number)
    }
}

struct SecretInput {
    file: Option<File>,
}
impl SecretInput {
    fn new(args: &Args) -> Result<Self> {
        let file = if let Some(number) = args.options.get("--password-fd") {
            #[cfg(target_os = "linux")]
            {
                use std::os::fd::FromRawFd;
                let fd = number.parse::<i32>().map_err(|_| Error::Input)?;
                if fd < 3 || unsafe { libc::fcntl(fd, libc::F_GETFD) } < 0 {
                    return Err(Error::Input);
                }
                // Do not allow an inherited secret descriptor into future children.
                if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
                    return Err(Error::Protection);
                }
                Some(unsafe { File::from_raw_fd(fd) })
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = number;
                return Err(Error::Unsupported);
            }
        } else {
            None
        };
        Ok(Self { file })
    }
    fn password(&mut self, prompt: &str) -> Result<Zeroizing<String>> {
        let value = if let Some(file) = &mut self.file {
            let mut bytes = Zeroizing::new(Vec::new());
            let mut byte = [0u8; 1];
            loop {
                let count = file.read(&mut byte).map_err(|_| Error::Input)?;
                if count == 0 || byte[0] == b'\n' {
                    break;
                }
                if bytes.len() == 1024 {
                    return Err(Error::Input);
                }
                bytes.push(byte[0]);
            }
            Zeroizing::new(
                String::from_utf8(std::mem::take(&mut *bytes)).map_err(|_| Error::Input)?,
            )
        } else {
            Zeroizing::new(rpassword::prompt_password(prompt).map_err(|_| Error::Protection)?)
        };
        if value.is_empty() || value.len() > 1024 || value.chars().any(char::is_control) {
            return Err(Error::Input);
        }
        Ok(value)
    }
    fn new_password(&mut self) -> Result<Zeroizing<String>> {
        let first = self.password("New vault password (at least 12 bytes): ")?;
        if first.len() < 12 {
            return Err(Error::Input);
        }
        let second = self.password("Confirm new vault password: ")?;
        use subtle::ConstantTimeEq;
        if !bool::from(first.as_bytes().ct_eq(second.as_bytes())) {
            return Err(Error::Authentication);
        }
        Ok(first)
    }
}
fn read_bounded(mut reader: impl Read, limit: usize) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    reader
        .by_ref()
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::Input)?;
    if bytes.is_empty() || bytes.len() > limit {
        return Err(Error::Input);
    }
    Ok(bytes)
}
fn read_policy(path: &Path) -> Result<Policy> {
    let bytes = read_bounded(File::open(path).map_err(|_| Error::Input)?, 64 * 1024)?;
    let mut policy: Policy = parse(&bytes)?;
    policy.validate()?;
    Ok(policy)
}
fn emit(value: &impl Serialize) -> Result<()> {
    let bytes = canonical(value)?;
    let mut output = std::io::stdout().lock();
    output
        .write_all(&bytes)
        .and_then(|_| output.write_all(b"\n"))
        .and_then(|_| output.flush())
        .map_err(|_| Error::Storage)
}
fn qualify() -> Result<()> {
    let status = secure::preflight();
    if status["ready"] != true {
        eprintln!("potbot identity: host needs at least 96 MiB of lockable memory; protected custody unavailable, no fallback");
        return Err(Error::Protection);
    }
    Ok(())
}
fn load(store: &Store) -> Result<Vault> {
    parse(&store.read()?)
}
fn status(operation: &str, vault: &Vault) -> Result<()> {
    emit(
        &json!({"schema_version":1,"operation":operation,"status":"complete","identity":vault.public()?}),
    )
}

fn execute() -> Result<i32> {
    let args = Args::parse()?;
    match args.command.as_str() {
        "--help" | "help" => {
            args.allowed(&[])?;
            println!("Agent custody: agent-provision, agent-public, agent-check, agent-sign, agent-rotate, agent-allocations. Agent operations require independently pinned --expect-operator/--expect-agent; provisioning uses --expect-signer for the operator. No raw-key export.");
            println!("potbot-identity preflight\npotbot-identity init --vault ABS_PATH --policy ABS_PATH\npotbot-identity public --vault ABS_PATH [--expect-signer HASH]\npotbot-identity sign --vault ABS_PATH --expect-signer HASH < public-request.json\npotbot-identity recovery --vault ABS_PATH --expect-signer HASH\npotbot-identity recover --vault ABS_PATH --policy ABS_PATH --expect-signer HASH\npotbot-identity change-password --vault ABS_PATH --expect-signer HASH\npotbot-identity backup --vault ABS_PATH --output ABS_PATH\nPasswords are prompted by native controlling TTY. Linux init/sign may receive a pre-opened private --password-fd N (N >= 3). No secret argv, environment, stdin, or stdout.");
        }
        "preflight" => {
            args.allowed(&[])?;
            let status = secure::preflight();
            let code = if status["ready"] == true { 0 } else { 78 };
            emit(&status)?;
            return Ok(code);
        }
        "agent-provision" => {
            args.allowed(&[
                "--vault",
                "--agent-vault",
                "--expect-signer",
                "--account-number",
                "--instance-id",
                "--group-id",
                "--group-epoch",
                "--password-fd",
            ])?;
            qualify()?;
            let request = ProvisionRequest {
                account_number: args.number("--account-number")?,
                instance_id: args.get("--instance-id")?.into(),
                group_id: args.get("--group-id")?.into(),
                group_epoch: args.number("--group-epoch")?,
            };
            request.validate()?;
            let operator_path = args.path("--vault")?;
            let agent_path = args.path("--agent-vault")?;
            if operator_path == agent_path {
                return Err(Error::Input);
            }
            let store = Store::open(&operator_path, false)?;
            let destination = Store::open(&agent_path, true)?;
            if destination.exists()? {
                return Err(Error::Storage);
            }
            let mut operator = load(&store)?;
            let expected = args.expected()?;
            if operator.public()?.signer_hash != expected {
                return Err(Error::Identity);
            }
            let mut input = SecretInput::new(&args)?;
            let operator_password = input.password("Operator vault password: ")?;
            let agent_password = input.new_password()?;
            let mut unlocked = operator.unlock(operator_password.as_bytes(), expected)?;
            let agent = unlocked.provision_agent(
                &mut operator,
                request,
                agent_password.as_bytes(),
                now()?,
            )?;
            // Reserve durably before the distributable ciphertext is written. A
            // failed second write leaves the account reserved, never reusable.
            store.write(&canonical(&operator)?, true)?;
            destination.write(&canonical(&agent)?, false)?;
            emit(&agent.public(expected)?)?;
        }
        "agent-allocations" => {
            args.allowed(&[
                "--vault",
                "--expect-signer",
                "--password-fd",
                "--offset",
                "--limit",
            ])?;
            qualify()?;
            let store = Store::open(&args.path("--vault")?, false)?;
            let operator = load(&store)?;
            let mut input = SecretInput::new(&args)?;
            let password = input.password("Operator vault password: ")?;
            let unlocked = operator.unlock(password.as_bytes(), args.expected()?)?;
            let offset = if args.options.contains_key("--offset") {
                args.number("--offset")? as usize
            } else {
                0
            };
            let limit = if args.options.contains_key("--limit") {
                args.number("--limit")? as usize
            } else {
                50
            };
            let allocations = unlocked.allocations();
            if offset > allocations.len() || limit == 0 || limit > 128 {
                return Err(Error::Input);
            }
            let end = (offset + limit).min(allocations.len());
            emit(
                &json!({"schema_version":1,"operator_fingerprint":operator.identity.signer_hash,
                "generation":operator.generation,"total":allocations.len(),"offset":offset,
                "next_offset":end,"has_more":end < allocations.len(),"allocations":&allocations[offset..end]}),
            )?;
        }
        "agent-public" | "agent-check" | "agent-sign" => {
            let private = args.command != "agent-public";
            args.allowed(if private {
                &[
                    "--vault",
                    "--expect-operator",
                    "--expect-agent",
                    "--password-fd",
                ]
            } else {
                &["--vault", "--expect-operator", "--expect-agent"]
            })?;
            if private {
                qualify()?;
            }
            let store = Store::open(&args.path("--vault")?, false)?;
            let mut agent: AgentVault = parse(&store.read()?)?;
            let expected_operator = args.get("--expect-operator")?;
            let public = agent.public(expected_operator)?;
            if args
                .options
                .get("--expect-agent")
                .is_some_and(|expected| expected != &public.agent.signer_hash)
            {
                return Err(Error::Identity);
            }
            if !private {
                emit(&public)?;
                return Ok(0);
            }
            let request = if args.command == "agent-sign" {
                Some(parse::<AgentSignRequest>(&read_bounded(
                    std::io::stdin().lock(),
                    16 * 1024,
                )?)?)
            } else {
                None
            };
            let mut input = SecretInput::new(&args)?;
            let password = input.password("Agent vault password: ")?;
            let clock = ClockGuard::new()?;
            let mut unlocked = agent.unlock(
                password.as_bytes(),
                expected_operator,
                args.get("--expect-agent")?,
            )?;
            if let Some(request) = request {
                let issued = clock.observed()?;
                let proof = unlocked.sign_reserved(&mut agent, request, issued)?;
                store.write(&canonical(&agent)?, true)?;
                if clock.observed()? >= proof.expires_at {
                    return Err(Error::Clock);
                }
                emit(&proof)?;
            } else {
                emit(&public)?;
            }
        }
        "agent-rotate" => {
            args.allowed(&[
                "--vault",
                "--agent-vault",
                "--expect-signer",
                "--expect-agent",
                "--group-epoch",
                "--password-fd",
            ])?;
            qualify()?;
            let operator_path = args.path("--vault")?;
            let agent_path = args.path("--agent-vault")?;
            if operator_path == agent_path {
                return Err(Error::Input);
            }
            let store = Store::open(&operator_path, false)?;
            let destination = Store::open(&agent_path, false)?;
            let mut operator = load(&store)?;
            let mut agent: AgentVault = parse(&destination.read()?)?;
            let expected = args.expected()?;
            agent.public(expected)?;
            let mut input = SecretInput::new(&args)?;
            let operator_password = input.password("Operator vault password: ")?;
            let agent_password = input.password("Agent vault password: ")?;
            let mut owner = operator.unlock(operator_password.as_bytes(), expected)?;
            let mut worker = agent.unlock(
                agent_password.as_bytes(),
                expected,
                args.get("--expect-agent")?,
            )?;
            owner.update_agent_group(
                &mut operator,
                &mut agent,
                &mut worker,
                args.number("--group-epoch")?,
            )?;
            store.write(&canonical(&operator)?, true)?;
            destination.write(&canonical(&agent)?, true)?;
            emit(&agent.public(expected)?)?;
        }
        "init" => {
            args.allowed(&["--vault", "--policy", "--password-fd"])?;
            qualify()?;
            let policy = read_policy(&args.path("--policy")?)?;
            let store = Store::open(&args.path("--vault")?, true)?;
            if store.exists()? {
                return Err(Error::Storage);
            }
            let mut input = SecretInput::new(&args)?;
            let password = input.new_password()?;
            let vault = Vault::create(password.as_bytes(), policy, now()?, None, None)?;
            store.write(&canonical(&vault)?, false)?;
            emit(&vault.public()?)?;
        }
        "public" => {
            args.allowed(&["--vault", "--expect-signer"])?;
            let store = Store::open(&args.path("--vault")?, false)?;
            let descriptor = load(&store)?.public()?;
            if args.options.contains_key("--expect-signer")
                && args.expected()? != descriptor.signer_hash
            {
                return Err(Error::Identity);
            }
            emit(&descriptor)?;
        }
        "sign" => {
            args.allowed(&["--vault", "--expect-signer", "--password-fd"])?;
            qualify()?;
            let expected = args.expected()?;
            let request: SignRequest = parse(&read_bounded(std::io::stdin().lock(), 16 * 1024)?)?;
            let store = Store::open(&args.path("--vault")?, false)?;
            let mut vault = load(&store)?;
            let mut input = SecretInput::new(&args)?;
            let password = input.password("Vault password: ")?;
            let clock = ClockGuard::new()?;
            let mut unlocked = vault.unlock(password.as_bytes(), expected)?;
            let issued = clock.observed()?;
            let bundle = unlocked.sign_reserved(&mut vault, request, issued)?;
            // A successful durable reservation is a prerequisite to releasing
            // either the hourly certificate or its message signature.
            store.write(&canonical(&vault)?, true)?;
            if epoch(clock.observed()?)? != epoch(issued)? {
                return Err(Error::Clock);
            }
            emit(&bundle)?;
        }
        "recovery" => {
            args.allowed(&["--vault", "--expect-signer"])?;
            qualify()?;
            let expected = args.expected()?;
            let store = Store::open(&args.path("--vault")?, false)?;
            let vault = load(&store)?;
            let mut input = SecretInput::new(&args)?;
            let confirm = input
                .password("Type SHOW RECOVERY to display the 24 words on this local terminal: ")?;
            if confirm.as_str() != "SHOW RECOVERY" {
                return Err(Error::Input);
            }
            let password = input.password("Vault password: ")?;
            let unlocked = vault.unlock(password.as_bytes(), expected)?;
            let words = unlocked.recovery_words()?;
            #[cfg(target_os = "linux")]
            let mut terminal = std::fs::OpenOptions::new()
                .write(true)
                .open("/dev/tty")
                .map_err(|_| Error::Protection)?;
            #[cfg(windows)]
            let mut terminal = std::fs::OpenOptions::new()
                .write(true)
                .open("CONOUT$")
                .map_err(|_| Error::Protection)?;
            #[cfg(not(any(target_os = "linux", windows)))]
            return Err(Error::Unsupported);
            #[cfg(any(target_os = "linux", windows))]
            terminal
                .write_all(b"\nPRIVATE RECOVERY WORDS - keep offline; never publish:\n")
                .and_then(|_| terminal.write_all(words.as_bytes()))
                .and_then(|_| terminal.write_all(b"\n"))
                .and_then(|_| terminal.flush())
                .map_err(|_| Error::Protection)?;
            status("recovery", &vault)?;
        }
        "recover" => {
            args.allowed(&["--vault", "--policy", "--expect-signer"])?;
            qualify()?;
            let expected = args.expected()?;
            let policy = read_policy(&args.path("--policy")?)?;
            let store = Store::open(&args.path("--vault")?, true)?;
            if store.exists()? {
                return Err(Error::Storage);
            }
            let mut input = SecretInput::new(&args)?;
            let words = input.password("24 English recovery words (hidden): ")?;
            let entropy = entropy_from_words(words.as_str())?;
            let password = input.new_password()?;
            let vault = Vault::create(
                password.as_bytes(),
                policy,
                now()?,
                Some(&entropy),
                Some(expected),
            )?;
            store.write(&canonical(&vault)?, false)?;
            emit(&vault.public()?)?;
        }
        "change-password" => {
            args.allowed(&["--vault", "--expect-signer"])?;
            qualify()?;
            let expected = args.expected()?;
            let store = Store::open(&args.path("--vault")?, false)?;
            let mut vault = load(&store)?;
            let mut input = SecretInput::new(&args)?;
            let password = input.password("Current vault password: ")?;
            // Collect all interactive input before materializing wallet keys.
            let replacement = input.new_password()?;
            let mut unlocked = vault.unlock(password.as_bytes(), expected)?;
            unlocked.change_password(&mut vault, replacement.as_bytes())?;
            store.write(&canonical(&vault)?, true)?;
            status("change-password", &vault)?;
        }
        "backup" => {
            args.allowed(&["--vault", "--output"])?;
            let source = args.path("--vault")?;
            let output = args.path("--output")?;
            if source == output {
                return Err(Error::Input);
            }
            let store = Store::open(&source, false)?;
            let bytes = store.read()?;
            let vault: Vault = parse(&bytes)?;
            vault.public()?;
            let destination = Store::open(&output, true)?;
            destination.write(&bytes, false)?;
            status("backup", &vault)?;
        }
        _ => return Err(Error::Input),
    }
    Ok(0)
}
