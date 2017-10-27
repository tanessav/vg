/** \file fizzbuzz.cpp
 *
 * Defines the "vg fizzbuzz" subcommand, which prints "fizz" 
 * for multiples of 3, "buzz" for multiples of 5, and "fizzbuzz"
 * for multiples of 3 and 5. 
 */

#include <iostream>
#include <string>
#include <vector>
#include <getopt.h>

#include "subcommand.hpp"
#include "../vg.hpp"
using namespace std;
using namespace vg;
using namespace vg::subcommand;

void help_fizzbuzz(char** argv){
	cerr << "usage: " << argv[0] << " fizzbuzz [options] -s " << endl
		 << "option: " << endl
		 << "           -s, --sort             sort results" << endl
		 << endl;
}

int main_fizzbuzz (int argc, char *argv[]) {
	vector<string> results;
	bool sortFlag = false;
	int c;
	while (true) {
		static struct option long_options[] = 
		{
			{"sort", no_argument, 0, 's'},
			{0, 0, 0, 0}
		};

		int option_index = 0;
		c = getopt_long (argc, argv, "s", long_options, &option_index);

		if (c == -1) 
			break;

		switch (c) 
		{
			case 's':
				sortFlag = true;
				break;
			case '?':
				help_fizzbuzz(argv);
				exit(1);
				break;
			default:
				abort();
		}	
	}

	for (int i = 1; i <= 100; i++) {
		if (i % 3 == 0 && i % 5 == 0) {
			results.push_back("fizzbuzz");
		}
		else if (i % 3 == 0) {
			results.push_back("fizz");
		}
		else if (i % 5 == 0) {
			results.push_back("buzz");
		}
		else {
			string number = to_string(i);
			results.push_back(number);
		}
	}

	if (sortFlag) {
		sort(results.begin(), results.end());
	}
	
	for (string number : results) {
		cout << number << endl;
	}
	return 0;
}

static Subcommand vg_fizzbuzz("fizzbuzz", "print 'fizz' for multiples of 3, 'buzz' for multiples of 5, and 'fizzbuzz' for multiples of 3 and 5", main_fizzbuzz);






