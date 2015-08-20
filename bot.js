var Discord = require('discord.js');
var request = require('request');
var fs = require('fs');

var config = {};
var raw = {};

function load_config() {
    config = JSON.parse(fs.readFileSync('config.json').toString());
}

function save_config() {
    fs.writeFileSync('config.json', JSON.stringify(config, null, 4), 'utf-8');
}

load_config();

var bot = new Discord.Client();
bot.login(config.username, config.password);

var commands = {};

var authority_prettify = {
    0: "User",
    1: "Moderator",
    2: "Admin",
    3: "Creator"
};

authority_prettify[-1] = "Banned";


function find_from(list, predicate) {
    for(var i = 0; i < list.length; ++i) {
        var value = list[i];
        if(predicate(value)) {
            return value;
        }
    }
    return null;
}

function toTitleCase(str) {
    return str.replace(/\w\S*/g, function(txt) {
        return txt.charAt(0).toUpperCase() + txt.substr(1).toLowerCase();
    });
}

function get_user_authority(userID) {
    return config.authority[userID] || 0;
}

// So.
// A command can be either a function or a dictionary
// A dictionary can have the following properties:
// 'hidden' -- specifies that it doesn't show up in !help
// 'authority' -- specifies that the userID must have x authority or higher, default 0.
// 'command' -- specifies the actual function to be called
// 'help' -- the help text given when !help is called
// 'help_args' -- something to prefix the !help output with

function get_command_from_message(message) {
    if(message.indexOf(config.command_prefix) !== 0) {
        return;
    }

    var command = message.substr(config.command_prefix.length);

    if(command in commands) {
        return commands[command];
    }
}

function command_is_hidden(key) {
    var command = commands[key];
    if(typeof(command) === 'object') {
        return command.hidden || command.authority == 3;
    }
    return false;
}

commands.help = {
    help: 'shows this message',
    hidden: true,
    command: function(message) {
        var text = 'available commands for you:\n';
        var authority = get_user_authority(message.author.id);
        for(var key in commands) {
            var command = commands[key];
            if(command.hidden) {
                continue;
            }

            if(command.authority) {
                if(authority < command.authority || command.authority === 3) {
                    continue;
                }
            }

            text = text.concat(config.command_prefix, key);
            if(command.help_args) {
                text = text.concat(' ', command.help_args);
            }

            if(command.help) {
                text = text.concat(' -- ', command.help);
            }

            text = text.concat('\n');
        }
        bot.startPM(message.author, text);
    }
};

commands.hello = {
    help: 'displays my intro message!',
    hidden: true,
    command: function(message) {
        bot.sendMessage(message.channel, "Hello! I'm a robot! Danny made me.");
    }
};

commands.random = {
    help: 'displays a random weapon, map, mode, or number',
    help_args: 'type',
    command: function(message) {
        var error_string = 'Random what? weapon, map, mode, or number? (e.g. !random weapon)'
        if(message.args.length < 1) {
            bot.sendMessage(message.channel, error_string);
            return;
        }

        var type = message.args[0].toLowerCase();
        if(type == 'weapon') {
            var index = Math.floor(Math.random() * config.splatoon.weapons.length);
            var weapon = config.splatoon.weapons[index]
            bot.sendMessage(message.channel, weapon.name + ' (sub: ' + weapon.sub + ', special: ' + weapon.special + ')');
        }
        else if(type == 'number') {
            bot.sendMessage(message.channel, Math.floor(Math.random() * 100).toString());
        }
        else if(type == 'map') {
            var index = Math.floor(Math.random() * config.splatoon.maps.length);
            bot.sendMessage(message.channel, config.splatoon.maps[index]);
        }
        else if(type == 'lenny') {
            // top sekret
            var lennies = [
                "( ͡° ͜ʖ ͡°)", "( ͠° ͟ʖ ͡°)", "ᕦ( ͡° ͜ʖ ͡°)ᕤ", "( ͡~ ͜ʖ ͡°)",
                "( ͡o ͜ʖ ͡o)", "͡(° ͜ʖ ͡ -)", "( ͡͡ ° ͜ ʖ ͡ °)﻿", "(ง ͠° ͟ل͜ ͡°)ง",
                "ヽ༼ຈل͜ຈ༽ﾉ"
            ];
            var index = Math.floor(Math.random() * lennies.length);
            bot.sendMessage(message.channel, lennies[index]);
        }
        else if(type == 'mode') {
            var modes = [
                'Turf War', 'Splat Zones', 'Rainmaker', 'Tower Control'
            ];
            var index = Math.floor(Math.random() * modes.length);
            bot.sendMessage(message.channel, modes[index]);
        }
        else {
            bot.sendMessage(message.channel, error_string);
        }
    }
};

commands.weapon = {
    help: 'displays weapons or a weapon info',
    help_args: 'query',
    command: function(message) {
        if(message.args.length < 1) {
            bot.sendMessage(message.channel, 'No query given. Try e.g. !weapon disruptor or !weapon splattershot jr. or !weapon inkstrike');
            return;
        }

        var input = message.args.join(' ').toLowerCase();
        if(input.length < 3) {
            bot.sendMessage(message.channel, 'The query must be at least 3 characters long');
            return;
        }

        // alright, first search for a weapon name.
        var weapons = config.splatoon.weapons;
        var result = weapons.filter(function(weapon) {
            return weapon.name.toLowerCase().indexOf(input) > -1;
        });

        if(result.length === 0) {
            // try sub search query
            result = weapons.filter(function(weapon) {
                return weapon.sub.toLowerCase().indexOf(input) > -1;
            });
        }

        if(result.length === 0) {
            // try special weapon query
            result = weapons.filter(function(weapon) {
                return weapon.special.toLowerCase().indexOf(input) > -1;
            });
        }

        if(result.length > 0) {
            // by now we must have found something...
            var text = 'Found the following weapons:\n';
            for(var i = 0; i < result.length; ++i) {
                var weapon = result[i];
                text = text.concat('Name: ', weapon.name, ' Sub: ', weapon.sub, ' Special: ', weapon.special, '\n');
            }

            bot.sendMessage(message.channel, text);
        }
        else {
            bot.sendMessage(message.channel, 'Sorry. The query "' + message.args.join(' ') + '" returned nothing.');
        }
    }
};

function get_profile_reply(profile) {
    var reply = 'Profile for ' + profile.name + ':\n';
    reply = reply.concat('NNID: ', !profile.nnid ? '*None found*' : profile.nnid, '\n');
    reply = reply.concat('Rank: ', !profile.rank ? '*None found*' : profile.rank, '\n');
    reply = reply.concat('Squad: ', !profile.squad ? '*None found*' : profile.squad, '\n');
    if(profile.weapon) {
        reply = reply.concat('Weapon: ', profile.weapon.name, ' (sub: ', profile.weapon.sub, ', special: ', profile.weapon.special, ')\n');
    }
    else {
        reply = reply.concat('Weapon: *None Found*\n');
    }
    return reply;
}

function create_profile_if_none_exists(user, force) {
    var profiles = config.splatoon.profiles;
    var userid = user.id;
    if(force || !(userid in profiles)) {
        profiles[userid] = { name: user.username, nnid: null, rank: null, squad: null, weapon: null };
        save_config();
    }
}

commands.profile = {
    help: 'manages your profile',
    help_args: 'action',
    command: function(message) {
        var error_message = 'Unknown action to do on profile.\n' +
                             'Valid actions are: get, nnid, rank, weapon, or delete'
        var type = message.args.length === 0 ? '' : message.args[0].toLowerCase();
        var profiles = config.splatoon.profiles;
        var userid = message.author.id;
        create_profile_if_none_exists(message.author);
        var profile = profiles[userid];

        // There are different operations you can do with a profile..
        // !profile get <user>
        // !profile nnid <nnid here>
        // !profile delete
        if(type == 'get' || type == '') {
            // !profile get user
            // gives the info for a specific user
            if(message.args.length < 2) {
                bot.sendMessage(message.channel, get_profile_reply(profile));
            }
            else {
                var username = message.args.slice(1).join(' ');
                var user = null;

                for(profileid in profiles) {
                    var value = profiles[profileid];
                    if(value.name == username) {
                        user = { id: profileid, name: value.name };
                        break;
                    }
                }

                if(user === null) {
                    bot.sendMessage(message.channel, 'User not found');
                    return;
                }
                create_profile_if_none_exists(user);
                bot.sendMessage(message.channel, get_profile_reply(profiles[user.id]));
            }
        }
        else if(type == 'nnid') {
            // !profile nnid <nnid here>
            // sets the NNID for a specific user
            if(message.args.length < 2) {
                bot.startPM(message.author, 'Missing your NNID to set to your profile');
            }
            else {
                var nnid = message.args[1];
                profile.nnid = nnid;
                save_config();
                bot.startPM(message.author, 'Your profile NNID is now set to ' + nnid);
            }
        }
        else if(type == 'delete') {
            // !profile delete [type]
            // deletes your profile
            if(message.args.length < 2) {
                create_profile_if_none_exists(message.author, true);
            }
            else {
                var delete_type = message.args[1].toLowerCase();
                if(!(delete_type in profile)) {
                    bot.startPM(message.author, 'Invalid delete action');
                }
                else {
                    profile[delete_type] = null;
                    save_config();
                }
            }
        }
        else if(type == 'rank') {
            var valid_ranks = ['C-', 'C', 'C+', 'B-', 'B', 'B+', 'A-', 'A', 'A+', 'S', 'S+'];
            if(message.args.length < 2) {
                bot.startPM(message.author, 'No Splatoon rank given');
            }
            else {
                var rank = message.args[1].toUpperCase();
                if(valid_ranks.indexOf(rank) !== -1) {
                    profile.rank = rank;
                    save_config();
                    bot.startPM(message.author, 'Your rank was successfully set to ' + rank);
                }
                else {
                    bot.startPM(message.author, 'Invalid rank given');
                }
            }
        }
        else if(type == 'squad') {
            if(message.args.length < 2) {
                bot.startPM(message.author, 'No squad given');
                return;
            }
            var squad = message.args.slice(1).join(' ');
            profile.squad = squad;
            save_config();
            bot.startPM(message.author, 'Your squad was successfully set to *' + squad + '*.');

        }
        else if(type == 'weapon') {
            if(message.args.length < 2) {
                bot.startPM(message.author, 'No weapon given');
            }
            else {
                var weapon = message.args.slice(1).join(' ').toLowerCase();
                var valid_weapon = find_from(config.splatoon.weapons, function(wep) {
                    return wep.name.toLowerCase() == weapon;
                });

                if(valid_weapon) {
                    profile.weapon = valid_weapon
                    save_config();
                    bot.startPM(message.author, 'Your main weapon was successfully set to ' + valid_weapon.name);
                }
                else {
                    bot.startPM(message.author, 'Invalid weapon given');
                }
            }
        }
        else {
            bot.sendMessage(message.channel, error_message);
        }
    }
};

function get_splatoon_map_callback(indices, current_channel) {
    return function(error, response, body) {
        if(error || response.statusCode != 200) {
            var error_message = "An error occurred. Tell Danny the error was " + error + ' [code: ' + response.statusCode + ']';
            error_message = error_message.concat('\nMaybe try again later.');
            bot.sendMessage(current_channel, error_message);
            return;
        }

        var data = JSON.parse(body);
        var schedule = data['schedule'];
        if(!schedule || schedule.length == 0) {
            bot.sendMessage(current_channel, "Maps could not be found...");
            return;
        }

        var result = [];
        var prefixes = {
            0: 'Current',
            1: 'Next',
            2: 'Last scheduled'
        };

        for(var i = 0; i < indices.length; ++i) {
            var index = indices[i];
            var prefix = prefixes[index];
            var current_maps = schedule[index];
            var ranked_name = current_maps.ranked.rulesEN;
            result.push(prefix + ' regular maps: ' + current_maps.regular.maps[0].nameEN + ' and ' + current_maps.regular.maps[1].nameEN);
            result.push(prefix + ' ' + ranked_name + ' maps: ' + current_maps.ranked.maps[0].nameEN + ' and ' + current_maps.ranked.maps[1].nameEN);
        }
        bot.sendMessage(current_channel, result.join('\n'));
    };
}

commands.maps = {
    help: 'shows the current Splatoon maps in rotation',
    command: function(message) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback([0], message.channel));
    }
};

commands.nextmaps = {
    help: 'shows the next Splatoon maps in rotation',
    command: function(message) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback([1], message.channel));
    }
};

commands.lastmaps = {
    help: 'shows the last Splatoon maps in schedule',
    hidden: true,
    command: function(message) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback([2], message.channel));
    }
}

commands.schedule = {
    help: 'shows the entire map schedule in Splatoon',
    hidden: true,
    command: function(message) {
        request(config.splatoon.schedule_url, get_splatoon_map_callback([0, 1, 2], message.channel));
    }
};

commands.quit = {
    authority: 3,
    command: function(message) {
        bot.logout();
        process.exit(0);
    }
};

commands.echo = {
    authority: 2,
    command: function(message) {
        bot.sendMessage(message.channel, message.args.join(' '));
    }
};

commands.choose = {
    help: 'helps choose between multiple choices',
    help_args: 'choices...',
    command: function(message) {
        if(message.args.length < 2) {
            bot.sendMessage(message.channel, 'Not enough choices to choose from... (e.g. !choose 1 2 3)');
            return;
        }

        var random_index = Math.floor(Math.random() * message.args.length);
        bot.sendMessage(message.channel, message.args[random_index]);
    }
};

commands.brand = {
    help: 'shows info about a splatoon brand',
    help_args: 'name',
    command: function(message) {
        var input = message.args.join(' ');
        var lower_case_input = input.toLowerCase();

        if(lower_case_input == 'list') {
            bot.sendMessage(message.channel, config.splatoon.brands.map(function(arg) { return arg.name; }).join(', '));
            return;
        }
        var brand = find_from(config.splatoon.brands, function(arg) {
            return arg.name.toLowerCase() == lower_case_input;
        });

        if(!brand) {
            bot.sendMessage(message.channel, 'Could not find brand "' + input + '".');
            return;
        }

        var result = '';

        if(brand.buffed == null || brand.nerfed == null) {
            result = 'The brand "' + brand.name + '" is neutral!\n';
        }
        else {
            result = 'The brand "' + brand.name + '" has ';
            result = result.concat('buffed ', config.splatoon.abilities[brand.buffed - 1], ' and nerfed ', config.splatoon.abilities[brand.nerfed - 1], ' probabilities\n');
        }

        bot.sendMessage(message.channel, result);
    }
};

commands.reloadconfig = {
    authority: 3,
    command: function(message) {
        load_config();
    }
};

commands.info = {
    help: 'shows information about the current user or another user',
    hidden: true,
    command: function(message) {
        var user;
        var text = '';
        if(!message.isPM()) {
            var server = message.channel.server;
            user = server.members.filter('username', message.args.join(' '), true) || message.author;
            var owner = server.members.filter('id', server.ownerID, true).username;
            text = 'Info for ' + user.mention() + ':\nYou\'re currently in #' + message.channel.name + ' in server ' + server.name;
            text = text.concat(' (', server.region, ')\n', 'The owner of this group is ', owner, '\n');
        }
        else {
            user = message.author;
            text = 'Info for ' + user.mention() + ':\nYou\'re currently in a private conversation with me!\n';
        }

        var authority = get_user_authority(user.id);
        text = text.concat('Your discord ID is: ' + user.id + '\nYour authority on me is **' + authority_prettify[authority] + '**');
        bot.sendMessage(message.channel, text);
    }
};

commands.cleanup = {
    help: 'cleans up past messages',
    authority: 1,
    help_args: '[messages]',
    command: function(message) {
        var amount = parseInt(message.args[0]) || 100;
        var text = '';
        var count = 0;
        var done;
        bot.getChannelLogs(message.channel, amount, function(error, logs) {
            for(message of logs.contents) {
                if(message.author.id === bot.user.id) {
                    ++count;
                    bot.deleteMessage(message);
                }
            }
            bot.deleteMessage(done);
            bot.sendMessage(message.channel, 'Cleanup has completed. ' + count + ' messages were deleted', false, true, { selfDestruct: 3000 });
        });
        bot.sendMessage(message.channel, 'Cleaning up...', function(error, arg) { done = arg; });
    }
};

commands.authority = {
    help: 'manages the authority of a user',
    authority: 1,
    help_args: 'new_authority username',
    command: function(message) {
        var server = message.channel.server;
        if(message.args[0] == 'list') {
            var text = '';
            for(key in authority_prettify) {
                text = text.concat(key, ' => ', authority_prettify[key], '\n');
            }
            bot.sendMessage(message.channel, text);
            return;
        }

        var authority = parseInt(message.args[0]) || 0;
        var author_authority = get_user_authority(message.author.id);
        var user = server.members.filter('username', message.args.slice(1).join(' '), true);

        if(!user) {
            bot.sendMessage(message.channel, 'User not found');
            return;
        }

        if(authority > author_authority || author_authority < get_user_authority(user.id)) {
            bot.sendMessage(message.channel, "You can't give someone authority higher than yours.");
            return;
        }

        if(!(authority in authority_prettify)) {
            bot.sendMessage(message.channel, "This authority does not exist.");
            return;
        }

        config.authority[user.id] = authority;
        save_config();
        bot.sendMessage(message.channel, user.username + ' now has an authority of **' + authority_prettify[authority] + '**');
    }
};

commands.timer = {
    help: 'reminds you after a certain amount of time',
    help_args: 'seconds [reminder]',
    command: function(message) {
        var time = parseInt(message.args[0]);
        if(isNaN(time)) {
            bot.sendMessage(message.channel, message.author.mention() + ', your time is incorrect. It has to be a number.');
            return;
        }

        var what = message.args.slice(1).join(' ');
        var reminder_message;
        var reminder_text = '';
        var complete_text = '';

        if(what) {
            reminder_text = message.author.mention() + ", I'll remind you to _\"" + what + "_\" in " + time + " seconds.";
            complete_text = message.author.mention() + ':\nTime is up! You asked to be remined for _"' + what + '_"!';
        }
        else {
            reminder_text = message.author.mention() + ", You've set a reminder in " + time + " seconds.";
            complete_text = message.author.mention() + ':\nTime is up! You asked to be reminded about something earlier.';
        }
        bot.sendMessage(message.channel, reminder_text, function(error, msg) { reminder_message = msg; });
        setTimeout(function() {
            bot.sendMessage(message.channel, complete_text);
            bot.deleteMessage(reminder_message);
        }, time * 1000);
    }
};

commands.coolkids = {
    help: 'are you cool?',
    command: function(message) {
        bot.sendMessage(message.channel, config.cool_kids.join(', '));
    }
};

commands.marie = {
    hidden: true,
    command: function(message) {
        bot.sendMessage(message.channel, 'http://i.stack.imgur.com/0OT9X.png');
    }
};

commands.splatwiki = {
    help: 'shows a page to the splatoon wiki',
    help_args: 'title',
    command: function(message) {
        var title = message.args.join(' ');
        if(title.length === 0) {
            bot.sendMessage(message.channel, 'Title to search for is required');
            return;
        }

        var url = 'http://splatoonwiki.org/w/index.php?title=' + encodeURIComponent(title);

        request(url, function(error, response, body) {
            if(error) {
                bot.sendMessage(message.channel, 'An error has occurred ' + error + '. Tell Danny.');
                return;
            }

            if(response.statusCode === 404) {
                // page not found so..
                bot.sendMessage(message.channel, 'Could not find a page with the title. Try searching: http://splatoonwiki.org/wiki/Special:Search/' + encodeURIComponent(title));
            }
            else if(response.statusCode === 200) {
                // actually found it so..
                bot.sendMessage(message.channel, url);
            }
        });
    }
};

commands.raw = {
    hidden: true,
    authority: 3,
    command: function(message) {
        bot.sendMessage(message.channel, JSON.stringify(raw));
    }
};

function message_callback(message) {
    // console.log(user + ' said: ' + message);
    var words = message.content.split(' ');
    var prefix = words[0];
    var command = get_command_from_message(prefix);

    if(command && message.author.id != bot.user.id) {
        message.args = words.slice(1);
        console.log(message.time + ': <' + message.author.username + ' @' + message.author.id + '> ' + message.content);
        if(typeof(command) == 'object') {
            var authority_required = command.authority || 0;
            if(get_user_authority(message.author.id) >= authority_required) {
                command.command(message);
            }
            else {
                bot.sendMessage(message.channel, "Sorry, you're not authorised to use this command");
            }
        }
        else {
            command(message);
        }
    }
}

bot.on('message', message_callback);
bot.on('ready', function() {
    console.log('Connected!\nLogging in as: ');
    console.log(bot.user.username);
    console.log(bot.user.id);
    console.log('-----');
});

bot.on('raw', function(e) {
    raw = JSON.parse(e.data);
});
