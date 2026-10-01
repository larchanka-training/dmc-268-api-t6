const CHECK_NAMES = ["lint", "typecheck", "test"] as const;

export function RequiredChecks() {
  return (
    <ul className="required-checks">
      {CHECK_NAMES.map((name) => (
        <li key={name} className="required-check">
          <span className="required-check__name">{name}</span>
        </li>
      ))}
    </ul>
  );
}
