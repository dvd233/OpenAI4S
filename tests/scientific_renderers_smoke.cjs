"use strict";

const assert = require("node:assert/strict");
const renderer = require("../openai4s/server/webui/scientific_renderers.js");

const fasta = renderer.parseSequence(">alpha first\nACGTACGT\n>beta\nACGU\n", "reads.fasta");
assert.equal(fasta.format, "FASTA");
assert.equal(fasta.records.length, 2);
assert.equal(fasta.total_length, 12);
assert.equal(fasta.records[0].description, "first");

const alignment = renderer.parseAlignment(
  "CLUSTAL W\n\nseq1    AC-GT\nseq2    ACTGT\n        ** **\n\nseq1    AA\nseq2    A-\n",
  "example.aln",
);
assert.equal(alignment.format, "Clustal");
assert.deepEqual(alignment.records.map((record) => record.sequence), ["AC-GTAA", "ACTGTA-"]);
assert.equal(alignment.columns, 7);

const genome = renderer.parseGenome(
  "chr1\t10\t25\tfeature-a\nchr1\t30\t45\tfeature-b\nchr2\t5\t9\tfeature-c\n",
  "track.bed",
);
assert.equal(genome.format, "BED");
assert.equal(genome.features.length, 3);
assert.equal(genome.chromosomes.length, 2);

// BED has three required fields and nine ordered optional fields. In
// particular, BED8-12 must not be reinterpreted as VCF or GFF by column count.
const bedFields = ["chr1", "100", "200", "peak", "960", "+", "100", "200", "0", "2", "25,25", "0,75"];
for (const filename of ["track.bed", "track.BED"]) {
  for (let columns = 3; columns <= 12; columns += 1) {
    const parsed = renderer.parseGenome(bedFields.slice(0, columns).join("\t"), filename);
    assert.equal(parsed.format, "BED", `${filename}: BED${columns}`);
    assert.equal(parsed.invalid, 0);
    assert.deepEqual(parsed.features, [{
      chrom: "chr1", start: 100, end: 200,
      label: columns >= 4 ? "peak" : "feature", type: "feature",
      strand: columns >= 6 ? "+" : "", score: columns >= 5 ? "960" : "",
    }], `${filename}: BED${columns} fields`);
  }
}

const gffFields = ["chr2", "5", "gene", "11", "25", ".", "-", ".", "ID=gene-b"];
for (const filename of ["track.gff", "track.GFF3"]) {
  const parsed = renderer.parseGenome(gffFields.join("\t"), filename);
  assert.equal(parsed.format, "GFF");
  assert.deepEqual(parsed.features, [{
    chrom: "chr2", start: 10, end: 25, label: "gene-b", type: "gene", strand: "-", score: ".",
  }]);
}

// GTF's display label changes after its first row; subsequent rows must
// retain GFF/GTF parsing rather than falling through to BED.
const gtfText = [
  'chr1\tensembl\texon\t101\t150\t.\t+\t.\tgene_id "gene-a";',
  'chr1\tensembl\texon\t176\t200\t.\t+\t.\tgene_id "gene-a";',
].join("\n");
const gtf = renderer.parseGenome(gtfText, "track.GTF");
assert.equal(gtf.format, "GTF");
assert.equal(gtf.invalid, 0);
assert.deepEqual(gtf.features.map(({ start, end, label }) => ({ start, end, label })), [
  { start: 100, end: 150, label: "gene-a" },
  { start: 175, end: 200, label: "gene-a" },
]);

const vcfFields = ["chr1", "101", "rs1", "A", "T", "42", "PASS", "."];
for (const fields of [vcfFields, [...vcfFields, "GT", "0/1"]]) {
  for (const filename of ["track.vcf", "track.VCF", "track.vcf.gz", "track.txt", ""]) {
    const parsed = renderer.parseGenome(fields.join("\t"), filename);
    assert.equal(parsed.format, "VCF");
    assert.deepEqual(parsed.features, [{
      chrom: "chr1", start: 100, end: 101, label: "rs1", type: "variant", strand: "", score: "42",
    }]);
  }
}

const bedGraph = renderer.parseGenome("chr1\t100\t200\t2.5", "track.bedGraph");
assert.equal(bedGraph.format, "bedGraph");
assert.deepEqual(bedGraph.features, [{
  chrom: "chr1", start: 100, end: 200, label: "2.5", type: "signal", strand: "", score: "2.5",
}]);
// Keep content inference available when the caller has no recognized format.
for (const filename of ["track.txt", ""]) {
  const inferredGff = renderer.parseGenome("chr2\tsource\tgene\t11\t25\t.\t-\t.\tID=gene-b", filename);
  assert.equal(inferredGff.format, "GFF");
  assert.equal(inferredGff.features[0].start, 10);
  const inferredBed = renderer.parseGenome("chr1\t100\t200\tpeak", filename);
  assert.equal(inferredBed.format, "BED");
  assert.equal(inferredBed.features[0].start, 100);
}

const molfile = [
  "Water",
  "  OpenAI4S",
  "",
  "  3  2  0  0  0  0            999 V2000",
  "    0.0000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0",
  "   -0.8000   -0.6000    0.0000 H   0  0  0  0  0  0  0  0  0  0  0  0",
  "    0.8000   -0.6000    0.0000 H   0  0  0  0  0  0  0  0  0  0  0  0",
  "  1  2  1  0  0  0  0",
  "  1  3  1  0  0  0  0",
  "M  END",
].join("\n");
const molecule = renderer.parseMolfile(molfile);
assert.equal(molecule.title, "Water");
assert.equal(molecule.atoms.length, 3);
assert.equal(molecule.bonds.length, 2);

const latex = renderer.latexPreview("\\section{Result}\nThe value is $$\\alpha \\leq 1$$.");
assert.deepEqual(latex[0], { kind: "heading", level: 1, text: "Result" });
assert.equal(latex.some((block) => block.kind === "math" && block.text.includes("α")), true);

const catalog = [{ renderer_id: "sequence" }, { renderer_id: "download" }];
assert.equal(renderer.rendererIdFromDescriptor({ renderer: { renderer_id: "sequence" } }, catalog), "sequence");
assert.equal(renderer.rendererIdFromDescriptor({ renderer: { renderer_id: "unknown-script" } }, catalog), "download");

console.log("scientific renderer parser smoke: ok");
