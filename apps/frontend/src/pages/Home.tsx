import { useAgents } from '../services/hooks';

export default function Home() {
  const { data } = useAgents();
  return (
    <div>
      <h1>Home Page</h1>
      {data && (
        <ul>
          {data.agents.map((a) => (
            <li key={a.id}>{a.symbol}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
