//! Explicit acceptance gate for the REAL production process and allocator.
//! All passwords, payloads and identities in this file are disposable test data.
//! It never disables protection. Run the ignored gate on a qualified Linux host.
#![cfg(target_os = "linux")]
use base64::{engine::general_purpose::STANDARD, Engine};
use potbot_identity::{canonical, parse, sha256_hex, PublicIdentity};
use serde_json::{json, Value};
use std::{
    fs::File,
    io::Write,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{fs::PermissionsExt, process::CommandExt},
    },
    process::{Command, Output, Stdio},
};

const PASSWORD: &str = "synthetic-test-password-only";
const AGENT_PASSWORD: &str = "synthetic-agent-password-only";
fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_potbot-identity")
}
fn invoke(arguments: &[&str], password: Option<&str>, request: Option<&[u8]>) -> Output {
    let mut command = Command::new(binary());
    command
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let (read, mut write) = if password.is_some() {
        let mut fds = [-1; 2];
        assert_eq!(unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) }, 0);
        let read = unsafe { File::from_raw_fd(fds[0]) };
        let write = unsafe { File::from_raw_fd(fds[1]) };
        let fd = read.as_raw_fd();
        assert!(fd >= 3);
        command.args(["--password-fd", "3"]);
        unsafe {
            command.pre_exec(move || {
                if fd != 3 && libc::dup2(fd, 3) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::fcntl(3, libc::F_SETFD, 0) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        (Some(read), Some(write))
    } else {
        (None, None)
    };
    let mut child = command.spawn().unwrap();
    drop(read);
    if let Some(value) = password {
        write.as_mut().unwrap().write_all(value.as_bytes()).unwrap();
    }
    drop(write);
    if let Some(value) = request {
        child.stdin.take().unwrap().write_all(value).unwrap();
    }
    drop(child.stdin.take());
    child.wait_with_output().unwrap()
}
fn public_json(output: &Output) -> Value {
    assert!(
        output.status.success(),
        "native process failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains(PASSWORD));
    assert!(!String::from_utf8_lossy(&output.stderr).contains(PASSWORD));
    assert!(!String::from_utf8_lossy(&output.stdout).contains(AGENT_PASSWORD));
    assert!(!String::from_utf8_lossy(&output.stderr).contains(AGENT_PASSWORD));
    parse(&output.stdout).unwrap()
}
fn verify(public: &str, signature: &str, prefix: &[u8], value: &Value) {
    let raw = STANDARD.decode(public).unwrap();
    assert_eq!(raw.len(), 32);
    let mut extended = [0u8; 64];
    extended[..32].copy_from_slice(&raw);
    let key = ed25519_bip32::XPub::from_bytes(extended);
    let signature =
        ed25519_bip32::Signature::<()>::from_slice(&STANDARD.decode(signature).unwrap()).unwrap();
    let mut unsigned = value.clone();
    unsigned.as_object_mut().unwrap().remove("signature");
    let mut message = prefix.to_vec();
    message.push(0);
    message.extend(canonical(&unsigned).unwrap());
    assert!(key.verify(&message, &signature));
}

#[test]
#[ignore = "real Agent custody acceptance requires qualified locked-memory host; no fallback"]
fn production_restricted_agent_custody_acceptance() {
    use potbot_identity::provisioning::{AgentProof, Membership};
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path().join("operator").join("vault.json");
    let agent = temporary.path().join("agent").join("vault.json");
    let duplicate = temporary.path().join("duplicate").join("vault.json");
    let offline = temporary.path().join("operator-offline.json");
    let policy = temporary.path().join("policy.json");
    let group = "11111111-1111-4111-8111-111111111111";
    std::fs::write(
        &policy,
        canonical(&json!({"workspace_id":group,
        "allowed_audiences":["custody-test"],"allowed_payload_types":["syn://type/ember"],
        "allowed_domains":["potbot.event.v2"]}))
        .unwrap(),
    )
    .unwrap();
    let (root_path, agent_path, duplicate_path) = (
        root.to_str().unwrap(),
        agent.to_str().unwrap(),
        duplicate.to_str().unwrap(),
    );
    let identity: PublicIdentity = serde_json::from_value(public_json(&invoke(
        &[
            "init",
            "--vault",
            root_path,
            "--policy",
            policy.to_str().unwrap(),
        ],
        Some(&format!("{PASSWORD}\n{PASSWORD}\n")),
        None,
    )))
    .unwrap();
    let pin = &identity.signer_hash;
    let provision = |path, account| {
        invoke(
            &[
                "agent-provision",
                "--vault",
                root_path,
                "--agent-vault",
                path,
                "--expect-signer",
                pin,
                "--account-number",
                account,
                "--instance-id",
                "22222222-2222-4222-8222-222222222222",
                "--group-id",
                group,
                "--group-epoch",
                "0",
            ],
            Some(&format!("{PASSWORD}\n{AGENT_PASSWORD}\n{AGENT_PASSWORD}\n")),
            None,
        )
    };
    let member: Membership =
        serde_json::from_value(public_json(&provision(agent_path, "7"))).unwrap();
    member.validate(pin).unwrap();
    let agent_pin = &member.agent.signer_hash;
    assert_ne!(agent_pin, pin);
    assert_eq!(
        std::fs::metadata(&agent).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        std::fs::metadata(agent.parent().unwrap())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    let agent_password = format!("{AGENT_PASSWORD}\n");
    let arguments = |command| {
        vec![
            command,
            "--vault",
            agent_path,
            "--expect-operator",
            pin,
            "--expect-agent",
            agent_pin,
        ]
    };
    // The REAL protected process reserves externally before releasing a DAG signature.
    let anchor = temporary.path().join("operator").join("dag-anchor.json");
    let anchor_path = anchor.to_str().unwrap();
    let owner_arguments = |command| {
        vec![
            command,
            "--vault",
            root_path,
            "--anchor",
            anchor_path,
            "--expect-signer",
            pin,
        ]
    };
    let owner_password = format!("{PASSWORD}\n");
    public_json(&invoke(
        &owner_arguments("dag-anchor-initialize"),
        Some(&owner_password),
        None,
    ));
    let original_vault = std::fs::read(&agent).unwrap();
    let mut body = json!({"profile":"syn.hashgraph-event/2","context_digest":"a".repeat(64),
        "creator":agent_pin,"sequence":"1","self_parent":null,"other_parent":null,
        "timestamp_ms":1000,"proposal":null});
    let intent = public_json(&invoke(
        &arguments("agent-dag-prepare"),
        Some(&agent_password),
        Some(&canonical(&body).unwrap()),
    ));
    assert!(intent.get("proof").is_none());
    let receipt = public_json(&invoke(
        &owner_arguments("dag-anchor-reserve"),
        Some(&owner_password),
        Some(&canonical(&intent).unwrap()),
    ));
    let committed = public_json(&invoke(
        &arguments("agent-dag-commit"),
        Some(&agent_password),
        Some(&canonical(&receipt).unwrap()),
    ));
    verify(
        &member.agent.public_key,
        committed["proof"]["signature"].as_str().unwrap(),
        b"syn-dag-event-proof-v1",
        &committed["proof"],
    );
    verify(
        &member.operator.public_key,
        receipt["signature"].as_str().unwrap(),
        b"syn-dag-anchor-receipt-v1",
        &receipt,
    );
    // Simulated vault restore excludes the independent operator journal and pin.
    std::fs::write(&agent, &original_vault).unwrap();
    body["timestamp_ms"] = 1001.into();
    let fork = public_json(&invoke(
        &arguments("agent-dag-prepare"),
        Some(&agent_password),
        Some(&canonical(&body).unwrap()),
    ));
    let refused = invoke(
        &owner_arguments("dag-anchor-reserve"),
        Some(&owner_password),
        Some(&canonical(&fork).unwrap()),
    );
    assert!(!refused.status.success());
    assert!(refused.stdout.is_empty());
    let exported = public_json(&invoke(
        &owner_arguments("dag-anchor-export"),
        Some(&owner_password),
        Some(
            &canonical(
                &json!({"node_hash":agent_pin,"context_digest":"a".repeat(64),"sequence":"1"}),
            )
            .unwrap(),
        ),
    ));
    assert_eq!(canonical(&exported).unwrap(), canonical(&receipt).unwrap());
    let recovered = public_json(&invoke(
        &arguments("agent-dag-commit"),
        Some(&agent_password),
        Some(&canonical(&exported).unwrap()),
    ));
    assert_eq!(
        canonical(&recovered).unwrap(),
        canonical(&committed).unwrap()
    );
    // Two restored copies race for the next parent. The operator's file locks
    // and permanent journal must allow exactly one public reservation.
    let head_one = std::fs::read(&agent).unwrap();
    body["sequence"] = "2".into();
    body["self_parent"] = receipt["intent"]["event_digest"].clone();
    body["timestamp_ms"] = 1002.into();
    let first = public_json(&invoke(
        &arguments("agent-dag-prepare"),
        Some(&agent_password),
        Some(&canonical(&body).unwrap()),
    ));
    std::fs::write(&agent, &head_one).unwrap();
    body["timestamp_ms"] = 1003.into();
    let second = public_json(&invoke(
        &arguments("agent-dag-prepare"),
        Some(&agent_password),
        Some(&canonical(&body).unwrap()),
    ));
    let races = std::thread::scope(|scope| {
        let one = scope.spawn(|| {
            invoke(
                &owner_arguments("dag-anchor-reserve"),
                Some(&owner_password),
                Some(&canonical(&first).unwrap()),
            )
        });
        let two = scope.spawn(|| {
            invoke(
                &owner_arguments("dag-anchor-reserve"),
                Some(&owner_password),
                Some(&canonical(&second).unwrap()),
            )
        });
        vec![one.join().unwrap(), two.join().unwrap()]
    });
    assert_eq!(races.iter().filter(|r| r.status.success()).count(), 1);
    assert!(races
        .iter()
        .filter(|r| !r.status.success())
        .all(|r| r.stdout.is_empty()));
    let winning = public_json(races.iter().find(|r| r.status.success()).unwrap());
    public_json(&invoke(
        &arguments("agent-dag-commit"),
        Some(&agent_password),
        Some(&canonical(&winning).unwrap()),
    ));
    // Losing the anchor must not silently create an empty replacement.
    std::fs::remove_file(&anchor).unwrap();
    let missing = invoke(
        &owner_arguments("dag-anchor-initialize"),
        Some(&owner_password),
        None,
    );
    assert!(!missing.status.success());
    assert!(!anchor.exists());
    // Agent commands have only the assigned vault; the operator file is unavailable.
    std::fs::rename(&root, &offline).unwrap();
    let checked = public_json(&invoke(
        &arguments("agent-check"),
        Some(&agent_password),
        None,
    ));
    assert_eq!(checked["agent"]["signer_hash"], *agent_pin);
    let request = |epoch, id| {
        canonical(&json!({"group_id":group,"group_epoch":epoch,
        "request_id":id,"payload_sha256":sha256_hex(b"agent production fixture"),
        "validity_seconds":300}))
        .unwrap()
    };
    let first = request(0, "33333333-3333-4333-8333-333333333333");
    let value = public_json(&invoke(
        &arguments("agent-sign"),
        Some(&agent_password),
        Some(&first),
    ));
    let proof: AgentProof = serde_json::from_value(value.clone()).unwrap();
    proof
        .verify(
            pin,
            group,
            0,
            &sha256_hex(b"agent production fixture"),
            proof.issued_at,
        )
        .unwrap();
    verify(
        &member.agent.public_key,
        value["signature"].as_str().unwrap(),
        b"syn-agent-proof-v1",
        &value,
    );
    let replay = invoke(
        &arguments("agent-sign"),
        Some(&agent_password),
        Some(&first),
    );
    assert!(!replay.status.success() && replay.stdout.is_empty());
    let wrong_password = invoke(
        &arguments("agent-check"),
        Some("incorrect synthetic password\n"),
        None,
    );
    assert!(!wrong_password.status.success() && wrong_password.stdout.is_empty());
    let mut wrong_pin = arguments("agent-check");
    *wrong_pin.last_mut().unwrap() =
        "0000000000000000000000000000000000000000000000000000000000000000";
    let rejected = invoke(&wrong_pin, Some(&agent_password), None);
    assert!(!rejected.status.success() && rejected.stdout.is_empty());
    let legacy = invoke(&["public", "--vault", agent_path], None, None);
    assert!(!legacy.status.success() && legacy.stdout.is_empty());
    std::fs::rename(&offline, &root).unwrap();
    let rejected = provision(duplicate_path, "7");
    assert!(!rejected.status.success() && rejected.stdout.is_empty() && !duplicate.exists());
    let listing = public_json(&invoke(
        &[
            "agent-allocations",
            "--vault",
            root_path,
            "--expect-signer",
            pin,
            "--offset",
            "0",
            "--limit",
            "1",
        ],
        Some(&format!("{PASSWORD}\n")),
        None,
    ));
    assert_eq!(listing["total"], 1);
    assert_eq!(listing["allocations"][0]["agent_fingerprint"], *agent_pin);
    let rotated = public_json(&invoke(
        &[
            "agent-rotate",
            "--vault",
            root_path,
            "--agent-vault",
            agent_path,
            "--expect-signer",
            pin,
            "--expect-agent",
            agent_pin,
            "--group-epoch",
            "1",
        ],
        Some(&format!("{PASSWORD}\n{AGENT_PASSWORD}\n")),
        None,
    ));
    assert_eq!(rotated["agent"], checked["agent"]);
    assert_eq!(rotated["group_epoch"], 1);
    let old = request(0, "44444444-4444-4444-8444-444444444444");
    let rejected = invoke(&arguments("agent-sign"), Some(&agent_password), Some(&old));
    assert!(!rejected.status.success() && rejected.stdout.is_empty());
    let next = request(1, "55555555-5555-4555-8555-555555555555");
    let next: AgentProof = serde_json::from_value(public_json(&invoke(
        &arguments("agent-sign"),
        Some(&agent_password),
        Some(&next),
    )))
    .unwrap();
    next.verify(
        pin,
        group,
        1,
        &sha256_hex(b"agent production fixture"),
        next.issued_at,
    )
    .unwrap();
    assert!(next
        .verify(pin, group, 0, &next.payload_sha256, next.issued_at)
        .is_err());
    let exported = public_json(&invoke(
        &["public", "--vault", root_path, "--expect-signer", pin],
        None,
        None,
    ));
    assert_eq!(exported, serde_json::to_value(identity).unwrap());
}

#[test]
#[ignore = "real custody acceptance requires >=96 MiB locked memory; failure never bypasses protection"]
fn production_password_custody_acceptance() {
    let check = invoke(&["preflight"], None, None);
    let preflight: Value = parse(&check.stdout).unwrap();
    assert!(
        check.status.success() && preflight["ready"] == true,
        "qualified native custody host required; preflight={} (no fallback)",
        preflight
    );
    let temporary = tempfile::tempdir().unwrap();
    let vault = temporary.path().join("private").join("identity.json");
    let policy = temporary.path().join("policy.json");
    let scope = json!({"workspace_id":"00000000-0000-4000-8000-000000000001","allowed_audiences":["custody-test"],"allowed_payload_types":["syn://type/ember"],"allowed_domains":["potbot.event.v2"]});
    std::fs::write(&policy, canonical(&scope).unwrap()).unwrap();
    let vault_path = vault.to_str().unwrap();
    let policy_path = policy.to_str().unwrap();
    let passwords = format!("{PASSWORD}\n{PASSWORD}\n");
    let init = invoke(
        &["init", "--vault", vault_path, "--policy", policy_path],
        Some(&passwords),
        None,
    );
    let identity: PublicIdentity = serde_json::from_value(public_json(&init)).unwrap();
    identity.validate().unwrap();
    assert_eq!(
        std::fs::metadata(&vault).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        std::fs::metadata(vault.parent().unwrap())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    let fingerprint = identity.signer_hash.as_str();
    let exported = invoke(
        &[
            "public",
            "--vault",
            vault_path,
            "--expect-signer",
            fingerprint,
        ],
        None,
        None,
    );
    assert_eq!(
        public_json(&exported),
        serde_json::to_value(&identity).unwrap()
    );
    let request=canonical(&json!({"workspace_id":scope["workspace_id"],"audience":"custody-test","domain":"potbot.event.v2","payload_type_ref":"syn://type/ember","payload_sha256":sha256_hex(b"custody acceptance payload"),"request_id":"00000000-0000-4000-8000-000000000002","validity_seconds":300})).unwrap();
    let arguments = [
        "sign",
        "--vault",
        vault_path,
        "--expect-signer",
        fingerprint,
    ];
    let password = format!("{PASSWORD}\n");
    // The broker must withhold a signature if its operation crosses an hour.
    // Start this acceptance operation outside the final half-minute to avoid
    // treating that correct refusal as an ordinary CI failure. This does not
    // change the production clock rule, and waits at most 31 seconds.
    let second_in_hour = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        % 3600;
    if second_in_hour >= 3570 {
        std::thread::sleep(std::time::Duration::from_secs(3601 - second_in_hour));
    }
    let signed = invoke(&arguments, Some(&password), Some(&request));
    let bundle = public_json(&signed);
    assert_eq!(bundle["identity"], serde_json::to_value(&identity).unwrap());
    assert_eq!(bundle["envelope"]["payload_type_ref"], "synny://type/ember");
    verify(
        &identity.public_key,
        bundle["delegation"]["signature"].as_str().unwrap(),
        b"potbot-hourly-delegation-v1",
        &bundle["delegation"],
    );
    verify(
        bundle["delegation"]["public_key"].as_str().unwrap(),
        bundle["envelope"]["signature"].as_str().unwrap(),
        b"potbot-signature-v2",
        &bundle["envelope"],
    );
    let encrypted: Value = parse(&std::fs::read(&vault).unwrap()).unwrap();
    assert_eq!(encrypted["generation"], 2);
    let replay = invoke(&arguments, Some(&password), Some(&request));
    assert!(!replay.status.success());
    assert!(replay.stdout.is_empty());
    let wrong = invoke(
        &arguments,
        Some("incorrect-test-password\n"),
        Some(&request),
    );
    assert_eq!(wrong.status.code(), Some(77));
    assert!(wrong.stdout.is_empty());
    let mut outside: Value = parse(&request).unwrap();
    outside["audience"] = "other-audience".into();
    outside["request_id"] = "00000000-0000-4000-8000-000000000003".into();
    let denied = invoke(
        &arguments,
        Some(&password),
        Some(&canonical(&outside).unwrap()),
    );
    assert_eq!(denied.status.code(), Some(64));
    assert!(denied.stdout.is_empty());
    let mut tampered = encrypted.clone();
    let ciphertext = tampered["payload"]["ciphertext_b64"].as_str().unwrap();
    let mut raw = STANDARD.decode(ciphertext).unwrap();
    raw[0] ^= 1;
    tampered["payload"]["ciphertext_b64"] = STANDARD.encode(raw).into();
    std::fs::write(&vault, canonical(&tampered).unwrap()).unwrap();
    let refused = invoke(&arguments, Some(&password), Some(&request));
    assert_eq!(refused.status.code(), Some(77));
    assert!(refused.stdout.is_empty());
}
