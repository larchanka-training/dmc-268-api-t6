import type { Film, Person } from "../src/types/swapi.js";

export function makeFilm(overrides: Partial<Film> = {}): Film {
  return {
    title: "A New Hope",
    episode_id: 4,
    opening_crawl: "It is a period of civil war...",
    director: "George Lucas",
    producer: "Gary Kurtz, Rick McCallum",
    release_date: "1977-05-25",
    characters: [
      "https://swapi.dev/api/people/1/",
      "https://swapi.dev/api/people/2/",
    ],
    planets: ["https://swapi.dev/api/planets/1/"],
    starships: [],
    vehicles: [],
    species: [],
    created: "2014-12-10T14:23:31.880000Z",
    edited: "2014-12-12T11:24:39.858000Z",
    url: "https://swapi.dev/api/films/1/",
    ...overrides,
  };
}

export function makePerson(overrides: Partial<Person> = {}): Person {
  return {
    name: "Luke Skywalker",
    birth_year: "19 BBY",
    eye_color: "Blue",
    gender: "Male",
    hair_color: "Blond",
    height: "172",
    homeworld: "https://swapi.dev/api/planets/1/",
    mass: "77",
    skin_color: "Fair",
    films: ["https://swapi.dev/api/films/1/"],
    species: ["https://swapi.dev/api/species/1/"],
    starships: [],
    vehicles: [],
    url: "https://swapi.dev/api/people/1/",
    created: "2014-12-09T13:50:51.644000Z",
    edited: "2014-12-10T13:52:43.172000Z",
    ...overrides,
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function paginated<T>(results: T[]): { count: number; next: null; previous: null; results: T[] } {
  return { count: results.length, next: null, previous: null, results };
}

/** Test data: episode V (film 2) and episode IV (film 1), out of order on purpose. */
export const filmOne = makeFilm();

export const filmTwo = makeFilm({
  title: "The Empire Strikes Back",
  episode_id: 5,
  release_date: "1980-05-21",
  director: "Irvin Kershner",
  characters: ["https://swapi.dev/api/people/1/"],
  url: "https://swapi.dev/api/films/2/",
});

export const personOne = makePerson();

export const personTwo = makePerson({
  name: "C-3PO",
  birth_year: "112 BBY",
  gender: "n/a",
  height: "167",
  mass: "75",
  url: "https://swapi.dev/api/people/2/",
});
