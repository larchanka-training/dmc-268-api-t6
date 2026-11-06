/** A single Star Wars film (episode). Field types follow the SWAPI documentation. */
export interface Film {
  title: string;
  episode_id: number;
  opening_crawl: string;
  director: string;
  producer: string;
  release_date: string;
  characters: string[];
  planets: string[];
  starships: string[];
  vehicles: string[];
  species: string[];
  created: string;
  edited: string;
  url: string;
}

/** A person/character within the Star Wars universe. */
export interface Person {
  name: string;
  birth_year: string;
  eye_color: string;
  gender: string;
  hair_color: string;
  height: string;
  homeworld: string;
  mass: string;
  skin_color: string;
  films: string[];
  species: string[];
  starships: string[];
  vehicles: string[];
  url: string;
  created: string;
  edited: string;
}

/** Standard SWAPI paginated list envelope. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

/**
 * Extracts the numeric resource id from a SWAPI hypermedia URL,
 * e.g. "https://swapi.dev/api/people/1/" -> 1 (resource "people").
 */
export function idFromUrl(url: string, resource: string): number | undefined {
  const match = url.match(new RegExp(`/${resource}/(\\d+)/?$`));
  return match?.[1] === undefined ? undefined : Number(match[1]);
}
